"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 -- residue semantics.

Asserts the verifier CANNOT today distinguish four genuinely different
residue states, and that the one operator command its own warning text
names structurally refuses the exact state it is named for.

DOES NOT MODIFY `app/realtime/segment.py` or `app/cli.py`.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

ENV = "demo"
UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def fields(i):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def _record_bytes(env_field, ordinal, previous_digest, segment_id):
    record = sg.build_record(
        envelope_fields=env_field, segment_id=segment_id, environment=ENV,
        previous_record_digest=previous_digest, receive_ordinal=ordinal)
    return cn.canonical_bytes(record) + b"\n", record["record_digest"]


def write_intact_residue(root, segment_id: str, n: int) -> None:
    """A genuinely INTACT, chain-valid, recoverable-but-uncommitted
    segment: real chained records, no manifest -- exactly what a live
    collector's currently-OPEN segment looks like on disk at any instant."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    (seg_dir / "events.jsonl.gz").write_bytes(gzip.compress(b"".join(lines)))


def write_torn_residue(root, segment_id: str, n: int) -> None:
    """A PARTIAL-TORN residue: the last record's bytes are truncated
    mid-write -- the realistic shape of a crash landing in the middle of
    `_fh.write(...)`."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    whole = b"".join(lines)
    compressed = gzip.compress(whole)
    # Truncate the COMPRESSED stream itself (a crash mid-fsync can leave a
    # torn gzip member, not merely a torn logical line).
    torn = compressed[: len(compressed) - 5]
    (seg_dir / "events.jsonl.gz").write_bytes(torn)


def write_malformed_residue(root, segment_id: str) -> None:
    """MALFORMED: not gzip at all -- a stray/corrupted file, not a torn
    write of a real one."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "events.jsonl.gz").write_bytes(b"not gzip data at all")


def write_deleted_record_residue(root, segment_id: str, n: int) -> None:
    """A residue with the SAME record count as an intact one, but with one
    MIDDLE record deleted -- so the chain is actually broken (a deletion),
    not merely incomplete. This is the state the milestone brief's own
    "indistinguishable from intact" finding is about: `chain_valid` is
    hardcoded False for a residue REGARDLESS of whether the chain the
    verifier would compute is actually valid or actually broken this way."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        if i == n // 2:
            continue  # deleted -- the chain no longer reconciles
        lines.append(line)
    (seg_dir / "events.jsonl.gz").write_bytes(gzip.compress(b"".join(lines)))


class TestChainValidIsNowReallyComputedForResidue:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 REPAIRED: `chain_valid` used
    to be hardcoded `False` for EVERY residue -- an actually intact,
    chain-valid residue and one with a deliberately deleted middle record
    were structurally indistinguishable. `verify_segment`'s `allow_open`
    path now runs a REAL `verify_chain` call over whatever was recovered,
    so the two cases below now genuinely differ."""

    def test_an_intact_chain_valid_residue_reports_chain_valid_true(
            self, tmp_path):
        root = tmp_path / "intact"
        root.mkdir()
        write_intact_residue(root, "seg-intact", 10)
        seg_dir = root / f"env={ENV}" / "segment=seg-intact"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.records_read == 10
        assert v.chain_valid is True
        assert v.residue_classification == sg.RESIDUE_RECOVERABLE_INTACT

    def test_a_residue_with_a_deleted_middle_record_reports_chain_valid_false(
            self, tmp_path):
        root = tmp_path / "deleted"
        root.mkdir()
        write_deleted_record_residue(root, "seg-deleted", 10)
        seg_dir = root / f"env={ENV}" / "segment=seg-deleted"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.chain_valid is False
        assert v.residue_classification == sg.RESIDUE_TORN_PARTIAL

    def test_intact_and_deleted_record_residue_are_now_distinguishable_by_chain_valid(
            self, tmp_path):
        """THE REPAIRED PROPERTY: `chain_valid` now carries real
        information for a residue -- True for the genuinely intact case,
        False for the genuinely tampered (deletion) case, so an operator
        reading `uncommitted_segment_detail` CAN use this field (and
        `residue_classification`) to tell them apart."""
        root = tmp_path / "compare"
        root.mkdir()
        write_intact_residue(root, "seg-a", 10)
        write_deleted_record_residue(root, "seg-b", 10)
        va = sg.verify_segment(root / f"env={ENV}" / "segment=seg-a",
                               environment=ENV, allow_open=True, root=root)
        vb = sg.verify_segment(root / f"env={ENV}" / "segment=seg-b",
                               environment=ENV, allow_open=True, root=root)
        assert va.chain_valid is True
        assert vb.chain_valid is False
        assert va.residue_classification != vb.residue_classification


class TestResidueIsNowClassifiedDistinctly:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 REPAIRED. Both cases below
    used to report `verdict: VALID` with NOTHING in the returned shape
    distinguishing "torn"/"malformed" residue from an ordinary in-progress
    OPEN segment. Residue STILL never gates the overall archive verdict
    (by design -- an in-progress OPEN segment from a live collector is
    ordinary, not a defect) -- but each state is now labelled."""

    def test_a_torn_residue_reports_valid_archive_but_is_labelled_torn(
            self, tmp_path):
        root = tmp_path / "torn"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_torn_residue(root, "seg-torn", 20)

        result = sg.verify_archive(root, environment=ENV)
        # Residue never gates the overall verdict.
        assert result["verdict"] == "VALID", result
        detail = result["uncommitted_segment_detail"]
        matched = next(d for d in detail if d["segment_id"] == "seg-torn")
        # Some prefix was recovered -- AND it is now labelled, both in the
        # structured `residue_classification` field and in `reasons`' text.
        assert matched["residue_classification"] == sg.RESIDUE_TORN_PARTIAL
        reasons_text = json.dumps(matched["reasons"]).lower()
        assert "torn" in reasons_text

    def test_a_malformed_non_gzip_residue_is_labelled_malformed_not_boilerplate(
            self, tmp_path):
        root = tmp_path / "malformed"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_malformed_residue(root, "seg-malformed")

        result = sg.verify_archive(root, environment=ENV)
        assert result["verdict"] == "VALID", (
            "a malformed (not-even-gzip) residue file should not gate the "
            f"overall archive verdict -- got {result}")
        detail = result["uncommitted_segment_detail"]
        matched = next(d for d in detail if d["segment_id"] == "seg-malformed")
        # REPAIRED: records_read == 0, but `residue_classification` and
        # `reasons` now distinguish "malformed, unparseable gzip" from an
        # ordinary, brand-new, genuinely empty OPEN segment -- no longer
        # byte-for-byte identical to "nothing written yet".
        assert matched["records_read"] == 0
        assert matched["residue_classification"] == sg.RESIDUE_MALFORMED
        reasons_text = json.dumps(matched["reasons"]).lower()
        assert "malformed" in reasons_text
        # No warning at the ARCHIVE level (gated on
        # `uncommitted_records_present`, which is 0 here -- residue that
        # recovered zero records never crosses that threshold) -- the
        # classification is visible per-segment in `uncommitted_segment_
        # detail`, which is the correct scope for a 0-record finding.
        assert not any("MALFORMED" in w or "CORRUPT" in w
                       for w in result["warnings"])

    def test_an_ordinary_empty_open_segment_is_not_misclassified_as_malformed(
            self, tmp_path):
        """Negative control: a genuinely empty (zero-byte) events file --
        what a brand-new OPEN segment looks like the instant it is created,
        before any record is ever written -- must NOT be classified as
        malformed just because it decodes to zero records."""
        root = tmp_path / "empty-open"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        seg_dir = root / f"env={ENV}" / "segment=seg-empty-open"
        seg_dir.mkdir(parents=True)
        (seg_dir / "events.jsonl.gz").write_bytes(b"")

        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.records_read == 0
        assert v.residue_classification == sg.RESIDUE_RECOVERABLE_INTACT
        assert v.chain_valid is True


class TestArchiveAdoptStructurallyRefusesTheResidueItsOwnWarningNames:
    def test_uncommitted_residue_warning_no_longer_recommends_archive_adopt(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A6 REPAIRED: the warning
        used to tell an operator to "resolve with 'archive-adopt'" for
        EVERY uncommitted residue -- a command that (proven below) refuses
        manifest-less residue unconditionally. The warning may still
        MENTION 'archive-adopt' (to explain why it does not apply), but
        must no longer recommend it as the resolution, and must say
        plainly that no operator command accepts this state."""
        root = tmp_path / "warning-text"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-warned", 5)
        result = sg.verify_archive(root, environment=ENV)
        warning = next(w for w in result["warnings"]
                       if "UNCOMMITTED_SEGMENT_RESIDUE" in w)
        assert "resolve with 'archive-adopt'" not in warning, warning
        assert "no operator command" in warning.lower(), warning

    def test_archive_adopt_refuses_that_exact_residue(self, tmp_path):
        """THE VIOLATION: the command the warning names is bounded (by its
        own docstring, deliberately) to `orphaned_committed_segments` --
        segments WITH a manifest the head does not mention. Uncommitted
        residue has NO manifest at all, so `archive_adopt` refuses it
        every time, for every uncommitted residue -- the warning points an
        operator at a command that cannot act on the state it is warning
        about."""
        from app.cli import archive_adopt

        root = tmp_path / "adopt-refuses"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-adopt-target", 5)

        result = sg.verify_archive(root, environment=ENV)
        assert any("UNCOMMITTED_SEGMENT_RESIDUE" in w
                   for w in result["warnings"])
        assert "seg-adopt-target" not in result["orphaned_committed_segments"]

        rc = archive_adopt(str(root), "seg-adopt-target", environment=ENV,
                           confirm=False, fmt="json")
        assert rc == 1, (
            "expected archive-adopt to refuse (rc=1) -- the command the "
            "UNCOMMITTED_SEGMENT_RESIDUE warning names cannot act on "
            "manifest-less residue at all")

    def test_general_gate_every_named_command_must_accept_the_state_it_names(
            self, tmp_path):
        """The general property this section's finding is an instance of:
        for every operator command named in a `reasons`/`warnings` string,
        that command must EXIST and must ACCEPT the exact state that named
        it. `archive-adopt`, named by `UNCOMMITTED_SEGMENT_RESIDUE`, fails
        this gate today (proven above). `archive-adopt`, named by
        `ORPHANED_COMMITTED_SEGMENT`, is the state it was actually built
        for and DOES accept it -- included here as the positive control so
        this gate is not vacuously satisfied by every command failing."""
        from app.cli import archive_adopt

        root = tmp_path / "positive-control"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-orphan",
                             partition_identity="p", commit_to_head=False)
        for i in range(3):
            assert w.submit(fields(i)) is None
        import time
        deadline = time.monotonic() + 2.0
        while w.accounting.written < 3 and time.monotonic() < deadline:
            time.sleep(0.005)
        w.close()   # manifest published, but commit_to_head=False -> orphaned

        result = sg.verify_archive(root, environment=ENV)
        assert "seg-orphan" in result["orphaned_committed_segments"], result

        rc = archive_adopt(str(root), "seg-orphan", environment=ENV,
                           confirm=False, fmt="json")
        assert rc == 0, (
            f"archive-adopt should ACCEPT (dry-run rc=0) the exact state "
            f"({'ORPHANED_COMMITTED_SEGMENT'!r}) it exists for: {rc}")
