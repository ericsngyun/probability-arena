"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 Parts 2-6 — B1/B2 falsification.

These are the acceptance tests for the archive head. The mechanism existed
before them; this file is what decides whether it does what it was built for.

Every fixture is produced through PRODUCTION APIs. Attacks mutate the produced
artifacts afterwards — nothing here hand-authors a manifest or a head in setup,
because a hand-built artifact proves only that the verifier agrees with the
test's idea of the format.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import canonical as cn
from app.realtime import archive_head as ah
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


def fields(i, ticker="KXA"):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": ticker, "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "delta_fp": "1.00",
                      "side": "no", "ts_ms": 1786150148065 + i},
        "normalized_event": {"raw_price_units": 5100},
    }


# --- PART 2: the head-bearing fixture ---------------------------------------------
def build_archive(root, *, segments=("A", "B", "C"), per=4, environment=ENV,
                  identity="kalshi-realtime"):
    """A canonical multi-segment archive, built the way production builds one."""
    _init_archive(root, environment)
    made = []
    for name in segments:
        w = sg.SegmentWriter(root, environment=environment,
                             segment_id=f"kalshi.seg-{name}",
                             partition_identity=f"venue=kalshi/date=2026-08-08/hour={name}",
                             subscription_metadata={"venue": "kalshi"},
                             archive_identity=identity)
        for i in range(per):
            assert w.submit(fields(i)) is None
        made.append(w.close())
    return made


@pytest.fixture
def archive(tmp_path):
    build_archive(tmp_path)
    assert sg.verify_archive(tmp_path, environment=ENV)["verdict"] == "VALID"
    return tmp_path


def seg_dir(root, name, environment=ENV):
    return root / f"env={environment}" / f"segment=kalshi.seg-{name}"


def verdict(root, environment=ENV):
    return sg.verify_archive(root, environment=environment)


def copy_archive(root, tmp_path, name):
    dest = tmp_path / f"copy-{name}"
    shutil.copytree(root, dest)
    return dest


class TestFixtureIsRealEvidence:
    def test_a_freshly_built_archive_verifies(self, archive):
        out = verdict(archive)
        assert out["verdict"] == "VALID", out["reasons"]
        assert out["segments"] == 3
        assert out["head_generation"] == 3
        assert out["records_read"] == 12

    def test_the_head_was_built_incrementally_not_by_discovery(self, archive):
        rec = ah.load_authoritative_head(archive, ENV).generation_record
        assert [e["segment_id"] for e in rec["segments"]] == [
            "kalshi.seg-A", "kalshi.seg-B", "kalshi.seg-C"]
        assert rec["generation"] == 3
        assert rec["head_digest"] == ah.head_digest_of(rec)
        # One immutable record per generation, 0..3 — genesis included.
        assert ah.present_generations(archive, ENV) == [0, 1, 2, 3]


# --- PART 3: B1 — the archive-history attack suite --------------------------------
class TestB1ArchiveHistoryAttacks:
    """Every one of these left the OLD verifier reporting VALID."""

    def _assert_invalid(self, root, *, why=None):
        out = verdict(root)
        assert out["verdict"] == "INVALID", (
            f"attack survived: {out.get('reasons')}")
        if why:
            assert any(why in r for r in out["reasons"]), out["reasons"]
        return out

    def test_attack_1_delete_first_segment(self, archive, tmp_path):
        c = copy_archive(archive, tmp_path, "a1")
        shutil.rmtree(seg_dir(c, "A"))
        out = self._assert_invalid(c, why="MISSING")
        assert out["records_expected"] == 12   # head still expects all of it

    def test_attack_2_delete_middle_segment(self, archive, tmp_path):
        """The primary B1 regression."""
        c = copy_archive(archive, tmp_path, "a2")
        shutil.rmtree(seg_dir(c, "B"))
        self._assert_invalid(c, why="MISSING")

    def test_attack_3_delete_final_segment(self, archive, tmp_path):
        """Proves the head prevents valid-prefix truncation."""
        c = copy_archive(archive, tmp_path, "a3")
        shutil.rmtree(seg_dir(c, "C"))
        self._assert_invalid(c, why="MISSING")

    def test_attack_4_delete_multiple_segments(self, archive, tmp_path):
        c = copy_archive(archive, tmp_path, "a4")
        shutil.rmtree(seg_dir(c, "B"))
        shutil.rmtree(seg_dir(c, "C"))
        out = self._assert_invalid(c, why="MISSING")
        assert sum("MISSING" in r for r in out["reasons"]) == 2

    def test_attack_5_valid_prefix_truncation(self, archive, tmp_path):
        """Leave a formerly-valid prefix and DO NOT touch the head."""
        c = copy_archive(archive, tmp_path, "a5")
        shutil.rmtree(seg_dir(c, "B"))
        shutil.rmtree(seg_dir(c, "C"))
        assert sg.verify_segment(seg_dir(c, "A"), environment=ENV).valid
        self._assert_invalid(c)

    def test_attack_6_graft_foreign_valid_segment(self, archive, tmp_path):
        foreign = tmp_path / "foreign"
        build_archive(foreign, segments=("Z",))
        assert sg.verify_segment(seg_dir(foreign, "Z"), environment=ENV).valid
        c = copy_archive(archive, tmp_path, "a6")
        shutil.copytree(seg_dir(foreign, "Z"), seg_dir(c, "Z"))
        out = self._assert_invalid(c, why="ORPHANED_COMMITTED_SEGMENT")
        assert "kalshi.seg-Z" in out["orphaned_committed_segments"]

    def test_attack_7_graft_and_delete(self, archive, tmp_path):
        foreign = tmp_path / "foreign7"
        build_archive(foreign, segments=("Z",))
        c = copy_archive(archive, tmp_path, "a7")
        shutil.rmtree(seg_dir(c, "B"))
        shutil.copytree(seg_dir(foreign, "Z"), seg_dir(c, "B"))
        self._assert_invalid(c)

    def test_attack_8_reorder_by_swapping_artifacts(self, archive, tmp_path):
        """Canonical verification must use committed history, not enumeration."""
        c = copy_archive(archive, tmp_path, "a8")
        a, b = seg_dir(c, "A"), seg_dir(c, "B")
        tmp = c / "_swap"
        shutil.move(str(a), str(tmp))
        shutil.move(str(b), str(a))
        shutil.move(str(tmp), str(b))
        self._assert_invalid(c)

    def test_attack_9_swap_in_a_foreign_head(self, archive, tmp_path):
        foreign = tmp_path / "foreign9"
        build_archive(foreign, segments=("A", "B", "C"))
        c = copy_archive(archive, tmp_path, "a9")
        shutil.copy(ah.current_head_path(foreign, ENV),
                    ah.current_head_path(c, ENV))
        self._assert_invalid(c)

    def test_attack_10_edit_segment_count_and_redigest(self, archive, tmp_path):
        c = copy_archive(archive, tmp_path, "a10")
        rec = ah.read_generation(c, ENV, 3)
        rec["segment_count"] = 2
        rec["head_digest"] = ah.head_digest_of(rec)   # self-consistent...
        ah.generation_path(c, ENV, 3).write_bytes(cn.canonical_bytes(rec))
        self._assert_invalid(c)                       # ...but not authoritative

    def test_attack_11_edit_terminal_digest_and_redigest(self, archive, tmp_path):
        c = copy_archive(archive, tmp_path, "a11")
        rec = ah.read_generation(c, ENV, 3)
        rec["terminal_segment_digest"] = "0" * 64
        rec["head_digest"] = ah.head_digest_of(rec)
        ah.generation_path(c, ENV, 3).write_bytes(cn.canonical_bytes(rec))
        self._assert_invalid(c)

    def test_attack_12_head_rollback_to_a_prior_generation(self, tmp_path):
        """A prior generation is genuinely valid; pointing at it to hide a later
        segment is not. The newer segment must surface rather than be dropped."""
        root = tmp_path / "roll"
        build_archive(root, segments=("A", "B"))
        build_archive(root, segments=("C",))
        assert verdict(root)["verdict"] == "VALID"
        ah._publish_current_head(root, ENV, ah.read_generation(root, ENV, 2))
        out = self._assert_invalid(root)
        # The generation-3 record is still on disk and newer than the pointer,
        # so this is a named, recoverable state rather than a silent rewrite.
        assert out["head_state"] == "STALE_HEAD"

    def test_attack_13_rollback_with_the_newer_generation_deleted(self, tmp_path):
        """The same rollback, with the evidence of it removed.

        Honest limit: with the tail segment gone, its generation record gone and
        the pointer rewritten, nothing INSIDE the root contradicts the shorter
        history. That is what `minimum_generation` is for — an anchor the
        archive cannot supply about itself."""
        root = tmp_path / "roll2"
        build_archive(root, segments=("A", "B"))
        build_archive(root, segments=("C",))
        assert verdict(root)["records_expected"] == 12
        shutil.rmtree(seg_dir(root, "C"))
        ah.generation_path(root, ENV, 3).unlink()
        ah._publish_current_head(root, ENV, ah.read_generation(root, ENV, 2))
        unanchored = verdict(root)
        assert unanchored["verdict"] == "VALID"       # stated, not hidden
        assert unanchored["records_expected"] == 8
        anchored = sg.verify_archive(root, environment=ENV, minimum_generation=3)
        assert anchored["verdict"] == "INVALID"
        assert any("HISTORY_TRUNCATED" in r for r in anchored["reasons"])

    def test_attack_14_a_generation_record_cannot_be_rewritten(self, tmp_path):
        """Create-once, enforced by `os.link` rather than by convention."""
        root = tmp_path / "immutable"
        build_archive(root, segments=("A", "B"))
        rec = ah.read_generation(root, ENV, 2)
        with pytest.raises(ah.ArchiveHeadError, match="created once"):
            ah._publish_generation(root, ENV, rec)

    def test_attack_15_deleting_an_interior_generation_is_caught(self, tmp_path):
        root = tmp_path / "gap"
        build_archive(root, segments=("A", "B", "C"))
        ah.generation_path(root, ENV, 2).unlink()
        out = self._assert_invalid(root)
        assert any("missing" in r for r in out["reasons"]), out["reasons"]


# --- PART 4: head-chain semantics --------------------------------------------------
class TestB2ProvenanceAttacks:
    """Each attack edits ONE manifest field, recomputes manifest_digest, and
    leaves the event file byte-identical. Every one used to return VALID."""

    def _relabel(self, root, name, field, value):
        d = seg_dir(root, name)
        m = cn.parse_canonical((d / sg.MANIFEST_FILENAME).read_bytes())
        before = (d / sg.EVENTS_FILENAME).read_bytes()
        m[field] = value
        m["manifest_digest"] = cn.digest_hex({k: m[k] for k in sg.MANIFEST_FIELDS})
        assert sg.verify_manifest_self_digest(m), "attack must be self-consistent"
        (d / sg.MANIFEST_FILENAME).write_bytes(cn.canonical_bytes(m))
        assert (d / sg.EVENTS_FILENAME).read_bytes() == before, "events changed"
        return sg.verify_segment(d, environment=ENV)

    @pytest.mark.parametrize("field,value", [
        ("segment_id", "kalshi.seg-OTHER"),
        ("environment", "production"),
        ("partition_identity", "venue=polymarket/date=1999-01-01/hour=00"),
        ("writer_version", "attacker/9"),
        ("close_status", "dirty"),
        ("previous_segment_digest", "0" * 64),
    ])
    def test_relabelling_a_constrained_field_is_rejected(self, archive, field, value):
        v = self._relabel(archive, "B", field, value)
        if v.valid:
            # previous_segment_digest is not verifiable from the segment alone —
            # only the head knows the real predecessor. The ARCHIVE must reject.
            assert verdict(archive)["verdict"] == "INVALID", (
                f"{field} relabel survived both segment and archive verification")
        else:
            assert v.reasons

    def test_subscription_metadata_relabel_is_rejected(self, archive):
        d = seg_dir(archive, "B")
        m = cn.parse_canonical((d / sg.MANIFEST_FILENAME).read_bytes())
        m["subscription_metadata"] = {"venue": "polymarket", "note": "fabricated"}
        m["subscription_metadata_digest"] = cn.digest_hex(m["subscription_metadata"])
        m["manifest_digest"] = cn.digest_hex({k: m[k] for k in sg.MANIFEST_FIELDS})
        (d / sg.MANIFEST_FILENAME).write_bytes(cn.canonical_bytes(m))
        v = sg.verify_segment(d, environment=ENV)
        assert not v.valid, "venue relabel survived"
        assert any("venue" in r for r in v.reasons)

    def test_closed_at_before_the_last_record_is_rejected(self, archive):
        v = self._relabel(archive, "B", "closed_at",
                          cn.canonical_datetime(NOW - timedelta(days=1)))
        assert not v.valid
        assert any("closed_at" in r for r in v.reasons)

    def test_opened_at_is_operational_and_not_record_bound(self, archive):
        """Deliberately NOT constrained against the first record. Writers are
        created lazily on first append, so the first accepted event genuinely
        predates the operational open timestamp. Asserting otherwise was a real
        bug in an earlier revision of this verifier."""
        v = self._relabel(archive, "B", "opened_at",
                          cn.canonical_datetime(NOW + timedelta(microseconds=2)))
        assert v.valid, v.reasons

    def test_a_relabelled_segment_also_fails_the_archive(self, archive):
        self._relabel(archive, "B", "partition_identity", "venue=x/date=y/hour=z")
        assert verdict(archive)["verdict"] == "INVALID"
