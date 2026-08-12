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


def _padded_fields(i, blob_chars: int = 1024):
    """`fields(i)` with a realistically-sized, POORLY COMPRESSIBLE payload
    blob. Deterministic (a SHA-256 keystream, not `os.urandom`) so a failure
    reproduces. Needed because the toy records elsewhere in this module are
    ~200 bytes and compress so well that 160 of them never fill zlib's
    internal output buffer -- so nothing partial ever reaches the disk and a
    test of the production flush cadence would measure nothing."""
    import hashlib

    blob = ""
    while len(blob) < blob_chars:
        blob += hashlib.sha256(f"{i}:{len(blob)}".encode()).hexdigest()
    f = fields(i)
    f["raw_event"] = {"price_dollars": "0.5100", "side": "no",
                      "blob": blob[:blob_chars]}
    return f


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


def write_zero_byte_residue(root, segment_id: str) -> None:
    """A brand-new OPEN segment: the events file exists and is empty."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "events.jsonl.gz").write_bytes(b"")


def write_two_byte_magic_residue(root, segment_id: str) -> None:
    """The two gzip magic bytes and nothing else -- a file that ANNOUNCES a
    gzip member and then stops before its header is even complete."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "events.jsonl.gz").write_bytes(b"\x1f\x8b")


def write_truncated_header_residue(root, segment_id: str, n: int = 5) -> None:
    """A REAL segment cut inside its 10-byte gzip header (the first 6 bytes of
    a complete stream). Distinct from the 2-byte magic shape in that these
    bytes genuinely are the head of real evidence."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    (seg_dir / "events.jsonl.gz").write_bytes(
        gzip.compress(b"".join(lines))[:6])


def write_mid_line_truncated_residue(root, segment_id: str, n: int,
                                     keep_records: int,
                                     shave_bytes: int = 40) -> None:
    """`write_crash_truncated_residue`, cut a few bytes SHORT of the flush
    boundary so the readable prefix ends INSIDE a record. This is what a live
    collector at the shipped `flush_every=256` produces routinely -- a
    readable prefix plus exactly ONE abandoned trailing line -- and it is
    byte-for-byte what a truncation that destroyed the tail record leaves."""
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
    (seg_dir / "events.jsonl.gz").write_bytes(
        raw.getvalue()[:cut - shave_bytes])


def write_only_unparseable_line_residue(root, segment_id: str) -> None:
    """A stream whose ONLY content is a single unparseable line, flushed but
    never terminated: `records_read == 0` and ONE abandoned line. Nothing at
    all was read from this file -- which is precisely why `UNTERMINATED`'s
    reason text may not open by claiming the prefix read is readable."""
    import io

    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    gz = gzip.GzipFile(fileobj=raw, mode="wb")
    gz.write(b"{not canonical json at all\n")
    gz.flush()
    (seg_dir / "events.jsonl.gz").write_bytes(raw.getvalue())


def write_mid_stream_garbage_residue(root, segment_id: str, n: int = 20,
                                     at: int = 10) -> None:
    """A complete stream with its trailer removed AND one unparseable line
    spliced into the middle, so the reader abandons every line from there on:
    MORE than one abandoned line, which stays TORN_PARTIAL."""
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        if i == at:
            lines.append(b"{not canonical json at all\n")
        lines.append(line)
    (seg_dir / "events.jsonl.gz").write_bytes(
        gzip.compress(b"".join(lines))[:-8])


def write_terminated_with_bad_tail_residue(root, segment_id: str,
                                           n: int) -> None:
    """A stream that DID reach a real gzip trailer, whose final line is not
    parseable canonical JSON: `n - 1` good records plus one bad tail, then
    `gzip.compress` in full.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3 -- THE GUARD'S
    `unterminated` HALF. `_classify_residue`'s one-abandoned-line rule is
    `not (unterminated and last_unreadable == 1)`. The `unterminated`
    conjunct is what confines the rule to streams whose end is genuinely
    unknowable. THIS shape is the one input where completeness IS
    establishable -- the trailer proves the writer finished -- so exactly one
    corrupt trailing line here is real content-level corruption and nothing
    else, and must stay `TORN_PARTIAL`.

    Dropping the `unterminated` conjunct is a single-token edit that the rest
    of the suite does not notice: measured, it makes this shape report
    `recoverable_intact`, `records_read=9`, `records_abandoned=1`, and print
    that "its content is complete as written" -- the fall-open class this
    milestone spent three rounds eliminating, asserted on the one input where
    the claim is unambiguously false.
    """
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    (seg_dir / "events.jsonl.gz").write_bytes(
        gzip.compress(b"".join(lines[:n - 1]) + b"{not json\n"))


def write_chain_broken_with_bad_tail_residue(root, segment_id: str,
                                             n: int) -> None:
    """An UNTERMINATED stream (trailer removed) whose records do not chain (a
    deleted middle record) AND whose final line is unparseable: exactly ONE
    abandoned line, so the `== 1` rule applies -- and the chain check below
    it is what must still catch this.

    KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3 -- THE FALL-THROUGH. The
    `== 1` branch deliberately does NOT return; it falls through to the chain
    check, so structural brokenness paired with an ordinary spill boundary is
    still reported. Making that branch `return RESIDUE_UNTERMINATED` instead
    is invisible to the rest of the suite: measured, deletion, reorder and
    duplicate residues each paired with a bad trailing line all then report
    `unterminated` -- the label a HEALTHY live collector gets -- where the
    real code correctly returns `torn_partial`.
    """
    seg_dir = root / f"env={ENV}" / f"segment={segment_id}"
    seg_dir.mkdir(parents=True, exist_ok=True)
    prev = sg.genesis_digest(segment_id=segment_id, environment=ENV)
    lines = []
    for i in range(n):
        line, prev = _record_bytes(fields(i), i, prev, segment_id)
        lines.append(line)
    spliced = b"".join(lines[:4] + lines[5:9]) + b"{bad\n"
    # `[:-8]` removes the 8-byte gzip trailer (CRC32 + ISIZE), which is what
    # makes the member unterminated without introducing any decode FAULT --
    # the same technique `write_mid_stream_garbage_residue` uses.
    (seg_dir / "events.jsonl.gz").write_bytes(gzip.compress(spliced)[:-8])


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
        assert v.chain_valid is True
        # RELAXED to the property this control's own docstring states. It
        # previously asserted equality to `RESIDUE_RECOVERABLE_INTACT`, which
        # made it circular as an argument for keeping that label: A8
        # redefined RECOVERABLE_INTACT to mean "the compressed stream reached
        # a real gzip trailer", and a zero-byte file contains no trailer, so
        # it was the one input that falsified the label's own definition. The
        # SAME relaxation was already made to the live-segment control in
        # this milestone, for the same reason. See
        # `TestA8ZeroByteResidueIsUnterminated` for the positive assertion.
        assert v.residue_classification not in (
            sg.RESIDUE_MALFORMED, sg.RESIDUE_TORN_PARTIAL), (
            "a genuinely empty, brand-new OPEN segment must not be reported "
            f"as corruption -- got {v.residue_classification!r}")


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


class TestA8ZeroByteResidueIsUnterminated:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2 -- BLOCKER 2.

    A8 redefined `RESIDUE_RECOVERABLE_INTACT` to mean "the compressed stream
    reached a real gzip trailer" and `verify_segment` prints that sentence
    verbatim. A zero-byte file contains no trailer, so it was the ONE input
    that falsified the label's own new definition -- an internal
    inconsistency this milestone introduced, not a pre-existing ambiguity.
    And "it is genuinely ambiguous, leave it" is precisely the argument A8
    rejects everywhere else: `RESIDUE_UNTERMINATED` exists BECAUSE live and
    truncated are indistinguishable from the bytes, and its reason text
    describes an empty file exactly.
    """

    def test_a_zero_byte_events_file_is_unterminated_not_intact(self, tmp_path):
        root = tmp_path / "zero-byte"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        seg_dir = root / f"env={ENV}" / "segment=seg-zero"
        seg_dir.mkdir(parents=True)
        (seg_dir / "events.jsonl.gz").write_bytes(b"")

        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.records_read == 0
        assert v.residue_classification != sg.RESIDUE_RECOVERABLE_INTACT, (
            "a zero-byte events file was certified 'the compressed stream "
            "reached a real gzip trailer' -- the single input for which that "
            "sentence is provably false")
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED
        text = json.dumps(v.reasons).lower()
        assert "end is unknown" in text, v.reasons

    def test_it_is_still_not_reported_as_corruption(self, tmp_path):
        """The property the old negative control actually cared about, kept:
        an ordinary brand-new OPEN segment must not be called malformed or
        torn."""
        root = tmp_path / "zero-byte-negctl"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        seg_dir = root / f"env={ENV}" / "segment=seg-zero-neg"
        seg_dir.mkdir(parents=True)
        (seg_dir / "events.jsonl.gz").write_bytes(b"")
        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert v.residue_classification not in (
            sg.RESIDUE_MALFORMED, sg.RESIDUE_TORN_PARTIAL)
        assert v.chain_valid is True
        assert sg.verify_archive(root, environment=ENV)["verdict"] == "VALID"


class TestA8LiveSegmentAtTheProductionFlushCadence:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 2 -- S1.

    Every live-segment residue test in this milestone used `flush_every=1`,
    where the file can only ever end on a record boundary and a partial line
    cannot exist. The SHIPPED DEFAULT is `flush_every=256` and `EventArchive`
    never overrides it, so in production zlib spills its buffer wherever it
    likes and the readable prefix legitimately ends INSIDE a record.
    `_classify_residue` checked `last_unreadable > 0 -> TORN_PARTIAL` BEFORE
    the unterminated check, so a perfectly healthy, uncorrupted live segment
    reported `torn_partial` -- measured 39/39 inspections of a healthy
    4,000-record segment, identical at d004c01. Pre-existing and fail-closed,
    but it defeated A4's stated goal on the only configuration that ships.
    """

    def test_a_healthy_live_segment_at_the_default_cadence_is_not_torn(
            self, tmp_path):
        """Inspected REPEATEDLY while it is still being written, which is
        what an operator actually does to a running collector. Record bodies
        are realistically sized (a payload blob, deterministic so the run
        reproduces): with the 200-byte toy records the rest of this module
        uses, 160 records between flushes compress to less than zlib's
        internal output buffer and nothing partial ever reaches the disk, so
        a small-record fixture would pass this test vacuously. Measured over
        40 inspections of a 4,000-record segment: 0 partial with toy records,
        12/40 with a 128-char blob, 34/40 with a 1024-char blob.
        """
        root = tmp_path / "live-default-cadence"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-live-256",
                             partition_identity="p", commit_to_head=False)
        assert w._flush_every == 256, (
            "this test exists to exercise the SHIPPED default; it is no "
            f"longer 256 but {w._flush_every}")
        seen = {}
        try:
            for i in range(2000):
                assert w.submit(_padded_fields(i)) is None
                if i % 100 != 99:
                    continue
                v = sg.verify_segment(w.dir, environment=ENV, allow_open=True,
                                      root=root)
                partial = sg.read_segment_records.last_unreadable
                seen[partial] = seen.get(partial, 0) + 1
                assert partial <= 1, (
                    "fixture assumption broken: a single interrupted zlib "
                    f"spill can leave at most ONE unreadable line, got "
                    f"{partial}")
                assert v.residue_classification != sg.RESIDUE_TORN_PARTIAL, (
                    "a HEALTHY, uncorrupted live segment at the SHIPPED "
                    "flush cadence is reported as torn -- A4's stated goal "
                    "holds only for flush_every=1, which nothing in "
                    f"production uses (records={v.records_read}, "
                    f"unreadable={partial})")
                assert v.residue_classification == sg.RESIDUE_UNTERMINATED
                assert v.chain_valid is True
        finally:
            w._release_lock()
        assert seen.get(1, 0) > 0, (
            "NOT ONE inspection ended inside a record, so this test never "
            f"exercised the production shape at all: {seen}. Re-tune the "
            "record size before trusting it.")

    def test_two_or_more_unreadable_lines_are_still_torn(self, tmp_path):
        """The relaxation is EXACTLY one trailing line -- all a single
        interrupted zlib spill can produce -- and no more. An unterminated
        stream with content-level corruption INSIDE it (so the reader
        abandons more than one line) still reports TORN_PARTIAL."""
        root = tmp_path / "still-torn"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        seg_id = "seg-mid-garbage"
        seg_dir = root / f"env={ENV}" / f"segment={seg_id}"
        seg_dir.mkdir(parents=True)
        prev = sg.genesis_digest(segment_id=seg_id, environment=ENV)
        lines = []
        for i in range(20):
            line, prev = _record_bytes(fields(i), i, prev, seg_id)
            if i == 10:
                lines.append(b"{not canonical json at all\n")
            lines.append(line)
        # A complete gzip stream with the trailer removed: no decode fault
        # (so `unterminated` is True, the branch under test), but the reader
        # abandons every line from the garbage one onwards.
        compressed = gzip.compress(b"".join(lines))
        (seg_dir / "events.jsonl.gz").write_bytes(compressed[:-8])

        v = sg.verify_segment(seg_dir, environment=ENV, allow_open=True,
                              root=root)
        assert sg.read_segment_records.decode_had_error is False, (
            "fixture mis-tuned: this must reach the no-fault (unterminated) "
            "branch, not the decode-error branch")
        assert sg.read_segment_records.last_unreadable > 1, (
            "fixture mis-tuned: expected more than one abandoned line, got "
            f"{sg.read_segment_records.last_unreadable}")
        assert v.residue_classification == sg.RESIDUE_TORN_PARTIAL


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


def _residue_corpus(root, monkeypatch):
    """Yield `(shape_name, SegmentVerdict)` for every residue SHAPE this
    module knows how to put on disk.

    Every verdict comes from a REAL `verify_segment` call over REAL bytes;
    `_classify_residue` is never called directly, because its contract is
    over `read_segment_records`' process-global diagnostics and calling it
    out of band would exercise a different function than production does.
    """
    import os

    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")

    def verdict(seg_id):
        return sg.verify_segment(root / f"env={ENV}" / f"segment={seg_id}",
                                 environment=ENV, allow_open=True, root=root)

    write_intact_residue(root, "c-complete", 10)
    yield "a complete, trailer-terminated stream", verdict("c-complete")

    write_crash_truncated_residue(root, "c-flushcut", 30, keep_records=6)
    yield "truncated at a flush boundary", verdict("c-flushcut")

    write_mid_line_truncated_residue(root, "c-midline", 30, keep_records=6)
    yield "truncated inside a record", verdict("c-midline")

    write_zero_byte_residue(root, "c-zero")
    yield "a 0-byte events file", verdict("c-zero")

    write_two_byte_magic_residue(root, "c-magic")
    yield "2 bytes of gzip magic and nothing else", verdict("c-magic")

    write_truncated_header_residue(root, "c-header")
    yield "truncated inside the gzip header", verdict("c-header")

    write_only_unparseable_line_residue(root, "c-oneline")
    yield "one unparseable line and nothing else", verdict("c-oneline")

    write_deleted_record_residue(root, "c-chain", 10)
    yield "chain broken by a deleted middle record", verdict("c-chain")

    write_mid_stream_garbage_residue(root, "c-garbage")
    yield "unparseable line spliced mid-stream", verdict("c-garbage")

    # The two shapes that gate `_classify_residue`'s one-abandoned-line rule
    # from both sides. See `TestTheOneAbandonedLineRuleIsGatedOnBothSides`,
    # which is what actually pins their labels; they live HERE so the closed-
    # set and totality assertions cover them too and so the corpus stays the
    # single place a residue shape is put on disk.
    write_terminated_with_bad_tail_residue(root, "c-term-badtail", 10)
    yield "trailer-terminated, one unparseable TAIL line", \
        verdict("c-term-badtail")

    write_chain_broken_with_bad_tail_residue(root, "c-chain-badtail", 10)
    yield "chain broken AND one unparseable tail line, unterminated", \
        verdict("c-chain-badtail")

    write_torn_residue(root, "c-torn", 30)
    yield "compressed payload corrupted mid-stream", verdict("c-torn")

    write_malformed_residue(root, "c-malformed")
    yield "not a gzip stream at all", verdict("c-malformed")

    write_intact_residue(root, "c-unreadable", 5)
    events = root / f"env={ENV}" / "segment=c-unreadable" / "events.jsonl.gz"
    os.chmod(events, 0o000)
    try:
        yield "unreadable (chmod 000)", verdict("c-unreadable")
    finally:
        os.chmod(events, 0o600)

    write_intact_residue(root, "c-overlimit", 10)
    # The record-count ceiling, lowered rather than a 500,000-record fixture
    # built: `_classify_residue` reads `capped`, which this sets identically.
    monkeypatch.setattr(sg, "_MAX_RESIDUE_DECODED_LINES", 3)
    try:
        yield "over the record-count safety ceiling", verdict("c-overlimit")
    finally:
        monkeypatch.undo()


class TestTheResidueClassificationSetIsClosedAndEnforced:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3 -- THE MISSING CONSUMER.

    `segment.RESIDUE_CLASSIFICATIONS` was added in the A8 round-2 commit with
    a docstring saying it exists "so nothing has to hand-enumerate" the
    labels -- and then had ZERO readers repo-wide: its own definition, and a
    comment telling the next reader not to re-enumerate the labels BECAUSE
    the constant exists. A closed set that nothing enforces is the exact
    anti-pattern this module names for itself ("a constructor argument
    written into the commit record and checked by nothing is a trap"), and it
    is how `SegmentVerdict.residue_classification`'s comment came to omit
    `RESIDUE_UNTERMINATED` in the very commit that added it.

    These tests are that consumer. Together they close the loop in both
    directions: no residue shape can produce a label outside the registry,
    and no label can enter the module without joining it.
    """

    def test_every_residue_shape_classifies_inside_the_closed_set(
            self, tmp_path, monkeypatch):
        """The behavioural half, over REAL bytes: complete, flush-boundary
        truncated, mid-record truncated, 0-byte, 2-byte magic, truncated
        header, one-unparseable-line, chain-broken, mid-stream garbage,
        payload-corrupted, non-gzip, chmod-000, over the safety ceiling,
        trailer-terminated-with-a-bad-tail, and
        chain-broken-with-a-bad-tail."""
        root = tmp_path / "closed-set"
        root.mkdir()
        checked = 0
        for shape, v in _residue_corpus(root, monkeypatch):
            assert v.residue_classification in sg.RESIDUE_CLASSIFICATIONS, (
                f"{shape!r} classified "
                f"{v.residue_classification!r}, which is not a member of "
                f"RESIDUE_CLASSIFICATIONS {sg.RESIDUE_CLASSIFICATIONS!r} -- "
                "the closed set the verifier's own documentation promises")
            checked += 1
        assert checked >= 15, (
            f"the corpus shrank to {checked} shapes; it is the only thing "
            "making this assertion non-vacuous")

    def test_the_corpus_reaches_every_registered_classification(
            self, tmp_path, monkeypatch):
        """Totality in the other direction: every REGISTERED label is
        produced by at least one real residue on disk. A label that no shape
        can reach is either unreachable code or a missing fixture, and both
        are findings -- this is what stopped `RESIDUE_UNSAFE_OVER_LIMIT` from
        having no end-to-end coverage at all."""
        root = tmp_path / "totality"
        root.mkdir()
        registered = set(sg.RESIDUE_CLASSIFICATIONS)
        seen = {v.residue_classification
                for _, v in _residue_corpus(root, monkeypatch)}
        assert seen == registered, (
            "the corpus and the registry disagree; unreached labels="
            f"{sorted(registered - seen)}, "
            f"unregistered labels={sorted(seen - registered)}")

    def test_classify_residue_can_only_return_a_registered_constant(self):
        """The structural half. Every `return` in `_classify_residue` must be
        a bare NAME that resolves to a registered label -- never a string
        literal, which is how a seventh classification would slip in without
        anything noticing."""
        import ast
        from pathlib import Path

        module = ast.parse(Path(sg.__file__).read_text())
        fn = next(n for n in module.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_classify_residue")
        returned = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            assert isinstance(node.value, ast.Name), (
                f"_classify_residue returns a non-Name expression at line "
                f"{node.lineno} ({ast.dump(node.value)[:120]}) -- every "
                "classification must be one of the named RESIDUE_* "
                "constants, or RESIDUE_CLASSIFICATIONS cannot enumerate the "
                "closed set and no consumer can check it")
            returned.append(node.value.id)
        assert len(returned) >= 6, (
            f"expected one return per classification, found {returned}")
        missing = object()
        for name in returned:
            value = getattr(sg, name, missing)
            assert value is not missing, (
                f"_classify_residue returns {name!r}, which is not a "
                "module-level constant")
            assert value in sg.RESIDUE_CLASSIFICATIONS, (
                f"_classify_residue can return {name!r} = {value!r}, which is "
                "NOT in RESIDUE_CLASSIFICATIONS -- a new label was added "
                "without registering it, and every consumer that trusts the "
                "closed set (starting with this module's own docstring) is "
                "now wrong")

    def test_every_residue_label_in_the_module_is_registered(self):
        """THE PIN THAT FAILS WHEN A LABEL IS ADDED AND NOT REGISTERED:
        every module-level `RESIDUE_*` string constant must be a member."""
        labels = {n: v for n, v in vars(sg).items()
                  if n.startswith("RESIDUE_") and isinstance(v, str)}
        assert labels, "no RESIDUE_* labels found -- this test is misaimed"
        defined = set(labels.values())
        registered = set(sg.RESIDUE_CLASSIFICATIONS)
        assert defined == registered, (
            "a residue label exists in the module but is not registered in "
            "RESIDUE_CLASSIFICATIONS (or vice versa): unregistered="
            f"{sorted(defined - registered)}, "
            f"registered-but-absent={sorted(registered - defined)}")
        assert len(sg.RESIDUE_CLASSIFICATIONS) == len(
            set(sg.RESIDUE_CLASSIFICATIONS)), (
            f"duplicate members: {sg.RESIDUE_CLASSIFICATIONS}")


class TestTheOneAbandonedLineRuleIsGatedOnBothSides:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3 -- THE RULE THIS COMMIT
    EXISTS TO MAKE AUDITABLE, PINNED.

    `_classify_residue` softens exactly one shape: `unterminated and
    last_unreadable == 1` does NOT return `TORN_PARTIAL`. Both halves of that
    guard, and the deliberate FALL-THROUGH after it, were provably
    unpinned -- the two most dangerous single-token edits to the rule each
    passed the entire suite:

    * dropping the `unterminated` conjunct
      (`if not (read_segment_records.last_unreadable == 1)`), and
    * turning the fall-through into `return RESIDUE_UNTERMINATED`.

    The closed-set and totality assertions above cannot catch either: both
    mutants still return REGISTERED labels, and both labels are still reached
    by some other shape, so `seen == registered` holds either way. Membership
    is not a behavioural pin. These two are.

    The shapes come from `_residue_corpus`, so they are the same REAL bytes
    and the same REAL `verify_segment` call the rest of the corpus uses; this
    class only adds the per-shape expectation the corpus has no place to
    carry.
    """

    @staticmethod
    def _by_shape(root, monkeypatch):
        return {shape: v for shape, v in _residue_corpus(root, monkeypatch)}

    def test_a_terminated_stream_with_one_bad_tail_line_stays_torn(
            self, tmp_path, monkeypatch):
        """The `unterminated` half. A REAL gzip trailer proves the writer
        finished, so one corrupt trailing line is content-level corruption
        and nothing else. Without the conjunct this is the one input where
        completeness IS establishable and `recoverable_intact` -- "its
        content is complete as written" -- is unambiguously false."""
        root = tmp_path / "term-badtail"
        root.mkdir()
        by_shape = self._by_shape(root, monkeypatch)
        v = by_shape["trailer-terminated, one unparseable TAIL line"]
        assert v.residue_classification == sg.RESIDUE_TORN_PARTIAL, (
            "a stream that reached a real gzip trailer with one unparseable "
            f"trailing line classified {v.residue_classification!r}: the "
            "`unterminated` conjunct of the one-abandoned-line guard is gone, "
            "so a completed write with a corrupt tail is now certified as "
            "complete")
        assert v.records_read == 9 and v.records_abandoned == 1, (
            f"the fixture no longer produces the shape it exists for: "
            f"records_read={v.records_read}, "
            f"records_abandoned={v.records_abandoned}")

    def test_one_bad_tail_line_still_falls_through_to_the_chain_check(
            self, tmp_path, monkeypatch):
        """The fall-through half. An ordinary spill boundary does not excuse
        a BROKEN CHAIN underneath it: the `== 1` branch must not return, or
        deletion/reorder/duplicate residues that happen to end mid-record all
        report `unterminated` -- the label a healthy live collector gets."""
        root = tmp_path / "chain-badtail"
        root.mkdir()
        by_shape = self._by_shape(root, monkeypatch)
        v = by_shape[
            "chain broken AND one unparseable tail line, unterminated"]
        assert v.records_abandoned == 1, (
            "the fixture must land on the `== 1` branch, or it does not "
            f"exercise the fall-through at all: {v.records_abandoned}")
        assert v.chain_valid is False, (
            "the fixture must have a genuinely broken chain, or the check it "
            "is falling through TO has nothing to find")
        assert v.residue_classification == sg.RESIDUE_TORN_PARTIAL, (
            f"a chain-broken residue classified {v.residue_classification!r} "
            "because its readable prefix happened to end inside a record: the "
            "`== 1` branch returned instead of falling through to the chain "
            "check, so a splice is now reported with the same label a healthy "
            "live segment gets")


class TestRecordsAbandonedIsAuditableInTheField:
    """KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 ROUND 3.

    A healthy live segment and one whose tail record was destroyed or spliced
    both report `residue_classification: "unterminated"` -- deliberately, and
    documented: from the bytes they are identical. The only thing separating
    them is HOW MANY lines the reader had to abandon, which
    `_classify_residue`'s `== 1` rule turns on and which was computed on
    every read and then thrown away. An operator saw only `records_read`, for
    which there is no baseline. It is now on the verdict and in
    `verify_archive`'s `uncommitted_segment_detail`.
    """

    def test_a_flush_boundary_truncation_abandons_nothing(self, tmp_path):
        root = tmp_path / "abandoned-none"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_crash_truncated_residue(root, "seg-fb", 30, keep_records=6)
        v = sg.verify_segment(root / f"env={ENV}" / "segment=seg-fb",
                              environment=ENV, allow_open=True, root=root)
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED
        assert v.records_read == 6
        assert v.records_abandoned == 0

    def test_a_mid_record_truncation_reports_exactly_one_abandoned(
            self, tmp_path):
        """The production flush cadence's ordinary shape, and equally the
        shape of a tail record destroyed on purpose. Same label, same
        `chain_valid` -- `records_abandoned` is the field that says which
        rule `_classify_residue` actually applied."""
        root = tmp_path / "abandoned-one"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_mid_line_truncated_residue(root, "seg-ml", 30, keep_records=6)
        v = sg.verify_segment(root / f"env={ENV}" / "segment=seg-ml",
                              environment=ENV, allow_open=True, root=root)
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED
        assert v.records_abandoned == 1, (
            "the `== 1` rule that keeps a healthy live segment out of "
            "TORN_PARTIAL is invisible to an operator unless the count is "
            f"reported: got {v.records_abandoned}")
        assert v.records_read == 5

    def test_more_than_one_abandoned_line_is_reported_as_more(self, tmp_path):
        """The other side of the `== 1` rule: TORN_PARTIAL, and a count an
        operator can see rather than infer."""
        root = tmp_path / "abandoned-many"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_mid_stream_garbage_residue(root, "seg-mg")
        v = sg.verify_segment(root / f"env={ENV}" / "segment=seg-mg",
                              environment=ENV, allow_open=True, root=root)
        assert v.residue_classification == sg.RESIDUE_TORN_PARTIAL
        assert v.records_abandoned > 1, v.records_abandoned

    def test_an_ordinary_committed_verdict_reports_zero(self, tmp_path):
        """Negative control: the field is residue-only and must not carry a
        stale process-global value into a committed verdict."""
        root = tmp_path / "committed"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-done",
                             partition_identity="p", commit_to_head=False,
                             flush_every=1)
        for i in range(3):
            assert w.submit(fields(i)) is None
        w.close()
        v = sg.verify_segment(w.dir, environment=ENV, root=root)
        assert v.valid is True, v.reasons
        assert v.records_abandoned == 0
        assert v.residue_classification is None

    def test_verify_archive_surfaces_it_per_uncommitted_segment(
            self, tmp_path):
        """The whole point: auditable IN THE FIELD, from the structured
        archive summary an operator actually reads, not only in tests."""
        root = tmp_path / "archive-detail"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_mid_line_truncated_residue(root, "seg-a1", 30, keep_records=6)
        write_crash_truncated_residue(root, "seg-a2", 30, keep_records=6)
        result = sg.verify_archive(root, environment=ENV)
        detail = {d["segment_id"]: d
                  for d in result["uncommitted_segment_detail"]}
        assert detail["seg-a1"]["records_abandoned"] == 1, detail["seg-a1"]
        assert detail["seg-a2"]["records_abandoned"] == 0, detail["seg-a2"]
        # ...and the two are otherwise indistinguishable, which is why the
        # field had to be added.
        assert (detail["seg-a1"]["residue_classification"]
                == detail["seg-a2"]["residue_classification"]
                == sg.RESIDUE_UNTERMINATED)
        assert detail["seg-a1"]["chain_valid"] is True
        assert detail["seg-a2"]["chain_valid"] is True

    def test_the_unterminated_reason_does_not_overstate_a_zero_record_read(
            self, tmp_path):
        """`UNTERMINATED`'s reason opened "the prefix read is readable and
        chain-valid". For a stream whose ONLY content is one unparseable line
        that is false in both halves: nothing was read, and `chain_valid` is
        `verify_chain([])` -- trivially true over zero records."""
        root = tmp_path / "overstated"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        write_only_unparseable_line_residue(root, "seg-ov")
        v = sg.verify_segment(root / f"env={ENV}" / "segment=seg-ov",
                              environment=ENV, allow_open=True, root=root)
        assert v.residue_classification == sg.RESIDUE_UNTERMINATED
        assert v.records_read == 0
        assert v.records_abandoned == 1
        text = json.dumps(v.reasons)
        assert "the prefix read is readable and chain-valid" not in text, (
            "the reason text asserts the prefix WAS read and IS readable for "
            f"a residue from which nothing was read at all: {v.reasons}")
        assert "records_abandoned" in text, (
            "the reason must point an operator at the counts that carry the "
            f"real information: {v.reasons}")
        # The properties the existing pins depend on are unchanged.
        assert "END IS UNKNOWN" in text, v.reasons
        assert "UNTERMINATED_RESIDUE" in text, v.reasons
