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
    """A PARTIAL-TORN residue: the compressed stream is genuinely CORRUPTED
    partway through -- a real `zlib.error`, not merely a clean truncation.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4: a plain truncation of a complete
    gzip stream (removing only trailing bytes) decompresses the ENTIRE
    deflate payload cleanly with zero decode faults -- `zlib.decompressobj`
    simply reports it ran out of input, not yet at `eof`. That is BYTE-FOR-
    BYTE the same signature a live, still-open segment has (`GzipFile.
    flush()` emits `Z_SYNC_FLUSH`, never a trailer -- see `segment.
    read_segment_records`'s `decode_had_error` diagnostic), and is
    genuinely, structurally indistinguishable from one: there is no fault to
    observe, only "the stream doesn't have a trailer yet". A REAL crash mid-
    fsync does not usually leave a byte-perfect prefix of a complete deflate
    block either -- it leaves a genuinely incomplete/invalid one -- so
    corrupting a run of bytes partway through the compressed stream (rather
    than only truncating the tail) is both the more faithful simulation of
    "a crash landed mid-write" AND the only way to produce the `zlib.error`
    that actually distinguishes "torn/corrupted" from "live, not yet
    closed".
    """
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    whole = b"".join(lines)
    compressed = bytearray(gzip.compress(whole))
    # Corrupt a short run of bytes partway through the deflate payload
    # (well before the trailer) -- a genuine decode fault, not a clean
    # truncation.
    mid = len(compressed) // 2
    for i in range(mid, min(mid + 20, len(compressed) - 8)):
        compressed[i] ^= 0xFF
    (seg_dir / "events.jsonl.gz").write_bytes(bytes(compressed))


def write_truncated_residue(root, segment_id: str, n: int, keep: int) -> None:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- RESTORED FIXTURE. A CLEAN
    BYTE TRUNCATION of a complete gzip stream: the first `keep` compressed
    bytes of an `n`-record segment, nothing altered, nothing appended.

    This fixture existed before A4 and was rewritten into `write_torn_
    residue`'s mid-payload bit-flip, on the argument that "a real crash does
    not usually leave a byte-perfect prefix". That argument is wrong, and
    rewriting it deleted the only coverage of the exact case A4's own change
    made fail OPEN. Page-granular writeback, a short `write()` and
    filesystem crash-truncation all leave a byte-perfect prefix; so does
    anyone deliberately removing the tail. It is the ordinary shape of a
    truncated file, not an exotic one.

    A clean truncation produces NO zlib fault -- `decompressobj` merely
    reports it has not reached `eof` -- so it is byte-for-byte
    indistinguishable from a live, still-open segment. That is precisely why
    the honest classification for BOTH is `RESIDUE_UNTERMINATED` and not
    `RESIDUE_RECOVERABLE_INTACT`.
    """
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    compressed = gzip.compress(b"".join(lines))
    (seg_dir / "events.jsonl.gz").write_bytes(compressed[:keep])


def write_crash_truncated_residue(root, segment_id: str, n: int,
                                  keep_records: int) -> None:
    """The FAITHFUL crash truncation: `n` records written through a real
    `gzip.GzipFile` with a `flush()` after each one (what `SegmentWriter`
    does on its flush cadence), then the file cut at the byte offset that
    existed immediately after record `keep_records`.

    Truncating at a FLUSH BOUNDARY is what a crashed collector actually
    leaves, and it is the case the classification gets wrong: the surviving
    prefix decodes with zero faults into exactly `keep_records` complete,
    parseable, correctly-chained lines. There is no partial line to notice
    and no zlib fault to observe -- only the missing trailer. Truncating at
    an arbitrary byte offset instead usually cuts mid-line, which
    `last_unreadable` catches as TORN_PARTIAL and which therefore does NOT
    exercise this defect.
    """
    import io

    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    raw = io.BytesIO()
    gz = gzip.GzipFile(fileobj=raw, mode="wb")
    cut = None
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        gz.write(line)
        gz.flush()
        if i + 1 == keep_records:
            cut = raw.tell()
    gz.close()
    assert cut is not None, "keep_records must be <= n"
    (seg_dir / "events.jsonl.gz").write_bytes(raw.getvalue()[:cut])


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


class TestA4LiveSegmentAndUnreadableResidue:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A4.

    Two residue-classification defects, both about `RESIDUE_RECOVERABLE_
    INTACT` being reachable for exactly the wrong reason or unreachable for
    exactly the wrong reason:

    1. `gzip.GzipFile.flush()` (what `SegmentWriter` calls on its flush
       cadence) emits a `Z_SYNC_FLUSH` marker, never a gzip trailer -- so a
       LIVE, still-open segment's bytes never reach `dec.eof`, and
       `RECOVERABLE_INTACT` was structurally UNREACHABLE for the single most
       common residue an operator actually inspects (a collector's current-
       hour segment, mid-collection). Fixed by distinguishing "ran out of
       input with zero decode faults" (a live segment) from "a real zlib/
       EOFError fault occurred" (genuine corruption) -- see `segment.
       read_segment_records`'s `decode_had_error` diagnostic.
    2. A residue that VISIBLY EXISTS but cannot be READ at all (permission
       denied) used to fall into the SAME diagnostic defaults as a
       genuinely empty, brand-new segment -- both reporting
       `stream_fully_decoded: True` -- and therefore verified `chain_ok`
       over zero recovered records and classified `RECOVERABLE_INTACT`: a
       fail-OPEN verdict for evidence nobody could prove the content of.
       Fixed with a dedicated `RESIDUE_UNREADABLE` classification, checked
       FIRST.
    """

    def test_a_genuinely_live_open_segment_is_not_reported_as_corruption(
            self, tmp_path):
        """A4's property, PRESERVED under A8's sixth label: a live segment
        must not be reported as torn/corrupt. A8 narrows the answer from
        `RECOVERABLE_INTACT` to `UNTERMINATED` -- see
        `TestA8UnterminatedResidue` for why "intact" was an assertion A4
        never established."""
        root = tmp_path / "live"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-live",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        try:
            for i in range(5):
                assert w.submit(fields(i)) is None
            # Deliberately NOT closed: this is what a live collector's
            # current-hour segment looks like on disk at any instant --
            # every record `flush()`ed (Z_SYNC_FLUSH, never a trailer), no
            # manifest.
            v = sg.verify_segment(w.dir, environment=ENV, allow_open=True,
                                  root=root)
            assert v.records_read == 5
            assert v.chain_valid is True
            assert v.residue_classification not in (
                sg.RESIDUE_TORN_PARTIAL, sg.RESIDUE_MALFORMED), (
                "a live, still-open segment with zero decode faults must "
                f"not be reported as corruption -- got "
                f"{v.residue_classification!r}")
            assert v.residue_classification == sg.RESIDUE_UNTERMINATED
        finally:
            w._release_lock()

    def test_an_unreadable_residue_fails_closed_not_recoverable_intact(
            self, tmp_path, monkeypatch):
        root = tmp_path / "unreadable"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-unreadable", 5)
        seg_dir = root / f"env={ENV}" / "segment=seg-unreadable"
        events_path = seg_dir / "events.jsonl.gz"
        import os
        os.chmod(events_path, 0o000)
        try:
            v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                                  root=root)
            assert v.residue_classification == sg.RESIDUE_UNREADABLE, (
                "a residue that exists but could not be read must FAIL "
                "CLOSED as RESIDUE_UNREADABLE, never RECOVERABLE_INTACT -- "
                f"got {v.residue_classification!r}")
            assert v.residue_classification != sg.RESIDUE_RECOVERABLE_INTACT
            assert v.valid is False
            reasons_text = json.dumps(v.reasons).lower()
            assert "unreadable" in reasons_text
        finally:
            os.chmod(events_path, 0o600)


class TestA8UnterminatedResidue:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 BLOCKER 4.

    A4 required `decode_had_error` before calling a stream torn, so that a
    live segment would stop being reported as corruption. It is true that a
    live segment produces no fault -- and it is equally true that a CLEAN
    TAIL TRUNCATION produces no fault either. A4 therefore did not only
    reclassify live segments: it made a segment with records physically
    removed report `recoverable_intact, chain_valid=True`, with no reason
    string of its own, so an operator saw the bare "segment has no
    manifest" boilerplate over a file truncated to hide evidence.

    Every test below FAILS if `RESIDUE_UNTERMINATED` is removed and the
    no-fault/no-trailer case falls back to `RESIDUE_RECOVERABLE_INTACT`.
    """

    def test_a_clean_truncation_is_not_certified_intact(self, tmp_path):
        """THE BLOCKER, directly: 30 records written, the stream truncated
        so only a prefix survives. Nothing about the surviving bytes is
        faulty -- which is exactly why "intact" is the wrong word for
        them."""
        root = tmp_path / "trunc"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_crash_truncated_residue(root, "seg-trunc", 30, keep_records=6)
        seg_dir = root / f"env={ENV}" / "segment=seg-trunc"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.records_read == 6, (
            "fixture mis-tuned: expected exactly the 6-record prefix, got "
            f"{v.records_read} of 30")
        assert v.chain_valid is True, (
            "the surviving prefix genuinely IS chain-valid -- that is the "
            "whole difficulty: nothing about these bytes is faulty, and 24 "
            "records are still gone")
        assert v.residue_classification != sg.RESIDUE_RECOVERABLE_INTACT, (
            "a residue with records physically removed must never be "
            "certified RECOVERABLE_INTACT -- that word asserts the stream "
            "reached a real gzip trailer, which this one did not")
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED

    @pytest.mark.parametrize("keep", [2, 8])
    def test_tiny_truncations_are_never_certified_intact(self, tmp_path, keep):
        """2-byte and 8-byte prefixes of a 30-record segment: no fault, no
        trailer, essentially nothing recovered. Reported `recoverable_
        intact, chain_valid=True` before A8."""
        root = tmp_path / f"tiny-{keep}"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_truncated_residue(root, f"seg-tiny-{keep}", 30, keep=keep)
        seg_dir = root / f"env={ENV}" / f"segment=seg-tiny-{keep}"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.residue_classification != sg.RESIDUE_RECOVERABLE_INTACT
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED

    def test_a_truncated_residue_now_carries_an_explanatory_reason(
            self, tmp_path):
        """`verify_segment` appended a reason for UNREADABLE /
        UNSAFE_OVER_LIMIT / MALFORMED / TORN_PARTIAL but NOT for
        RECOVERABLE_INTACT, so this state presented as boilerplate."""
        root = tmp_path / "trunc-reason"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_crash_truncated_residue(root, "seg-trunc-reason", 30,
                                      keep_records=6)
        seg_dir = root / f"env={ENV}" / "segment=seg-trunc-reason"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        text = json.dumps(v.reasons).lower()
        assert "unterminated" in text, v.reasons
        assert "end is unknown" in text, v.reasons
        assert v.reasons != [
            "segment has no manifest and is therefore not committed"]

    def test_a_genuinely_terminated_residue_still_reports_intact(self, tmp_path):
        """The positive control, so `RECOVERABLE_INTACT` is not merely made
        unreachable: a complete `gzip.compress` stream DOES reach a
        trailer, and still classifies intact."""
        root = tmp_path / "terminated"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-terminated", 10)
        seg_dir = root / f"env={ENV}" / "segment=seg-terminated"
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.records_read == 10
        assert v.residue_classification == sg.RESIDUE_RECOVERABLE_INTACT
        text = json.dumps(v.reasons).lower()
        assert "gzip trailer" in text, v.reasons

    def test_truncation_and_a_live_segment_are_reported_with_the_same_label(
            self, tmp_path):
        """Deliberate, and stated: from the BYTES they are identical, so a
        verifier that claimed to tell them apart would be lying. What A8
        fixes is that neither is called `intact`."""
        root = tmp_path / "same-label"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_crash_truncated_residue(root, "seg-t", 30, keep_records=6)
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-l",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        try:
            for i in range(5):
                assert w.submit(fields(i)) is None
            a = sg.verify_segment(root / f"env={ENV}" / "segment=seg-t",
                                  environment=ENV, allow_open=True, root=root)
            b = sg.verify_segment(w.dir, environment=ENV, allow_open=True,
                                  root=root)
            assert a.residue_classification == b.residue_classification
            assert a.residue_classification == sg.RESIDUE_UNTERMINATED
        finally:
            w._release_lock()


class TestA8UnreadableResidueChainValidity:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 BLOCKER 5.

    `verify_chain([])` returns `ok=True` -- an empty chain is trivially
    consistent -- and `verify_segment` copied that into `chain_valid`
    unconditionally. Against a live segment holding four durable records,
    `chmod 000` produced `records_read: 0, chain_valid: true,
    uncommitted_records_present: 0, verdict: VALID, warnings: []`. The
    free-text reason said "unreadable"; the FIELD said the chain was valid,
    and the archive summary said nothing at all.
    """

    def test_chain_valid_is_false_for_an_unreadable_residue(self, tmp_path):
        import os
        root = tmp_path / "unreadable-field"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-cv", 4)
        seg_dir = root / f"env={ENV}" / "segment=seg-cv"
        events_path = seg_dir / "events.jsonl.gz"
        os.chmod(events_path, 0o000)
        try:
            v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                                  root=root)
            assert v.residue_classification == sg.RESIDUE_UNREADABLE
            assert v.chain_valid is False, (
                "a consumer branching on the FIELD -- which is the entire "
                "reason the field exists -- must not read `true` for a "
                "chain nobody was able to verify")
        finally:
            os.chmod(events_path, 0o600)

    def test_the_archive_summary_cannot_report_valid_with_no_warnings(
            self, tmp_path):
        import os
        root = tmp_path / "unreadable-archive"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-cv2", 4)
        events_path = (root / f"env={ENV}" / "segment=seg-cv2"
                       / "events.jsonl.gz")
        os.chmod(events_path, 0o000)
        try:
            result = sg.verify_archive(root, environment=ENV)
            # Residue still never GATES the verdict -- that is A6's design
            # decision and A8 does not change it. What it may no longer do
            # is be invisible.
            assert any("UNPROVEN_RESIDUE_CONTENT" in w
                       for w in result["warnings"]), result["warnings"]
            detail = next(d for d in result["uncommitted_segment_detail"]
                          if d["segment_id"] == "seg-cv2")
            assert detail["chain_valid"] is False
            assert detail["residue_classification"] == sg.RESIDUE_UNREADABLE
        finally:
            os.chmod(events_path, 0o600)

    def test_a_readable_residue_does_not_raise_the_unproven_warning(
            self, tmp_path):
        """Negative control: the warning must fire for unproven content,
        not for every residue."""
        root = tmp_path / "readable-archive"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_intact_residue(root, "seg-ok", 4)
        result = sg.verify_archive(root, environment=ENV)
        assert not any("UNPROVEN_RESIDUE_CONTENT" in w
                       for w in result["warnings"]), result["warnings"]


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
