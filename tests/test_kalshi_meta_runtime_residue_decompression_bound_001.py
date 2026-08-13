"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5 -- residue decompression bound.

REPAIRED. This file originally reproduced the reviewer's finding: a small,
realistic, gzip-compressed "residue" events file -- exactly what `mkdir`
plus a dropped file gives anyone with write access to the archive root, no
manifest and no writer lock required -- expanded, UNCAPPED, to hundreds of
megabytes in RAM when `segment.read_segment_records` (reached via
`verify_archive`'s own residue-inspection loop) decompressed it. `segment.
_decompress_prefix`/`_salvage_prefix` now stream through `zlib.decompressobj.
decompress(chunk, max_length)` against a shared, cumulative-across-members
`_MAX_RESIDUE_DECODED_BYTES` ceiling (and `read_segment_records` also caps
the number of parsed records), so a 500 MB-target bomb -- previously
unbounded, proportional to whatever the attacker's compressed input claimed
-- now peaks at a FIXED ceiling regardless of the target, and
`read_segment_records.capped` records that the read was cut off by the
ceiling rather than reaching a genuine stream EOF.

Measured OUT OF PROCESS, with a PARENT-ENFORCED timeout, so a much larger
bomb than this file actually constructs (the reviewer measured a 2 GiB
expansion SIGKILLed at 120s) could never hang this suite even if attempted.

DOES NOT MODIFY `app/realtime/evidence_fs.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.meta_runtime.parent_timeout import run_with_parent_timeout
from tests.meta_runtime.residue_bomb import (
    build_residue_segment, make_decompression_bomb, measurement_script,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV = "demo"

# ru_maxrss is bytes on macOS/BSD, kilobytes on Linux (documented
# platform difference in `getrusage(2)`) -- normalised to bytes here so the
# amplification ratio this file asserts is portable across CI/dev hosts.
_RU_MAXRSS_UNIT = 1 if sys.platform == "darwin" else 1024


class TestDecompressionIsNowBounded:
    def test_a_small_compressed_residue_no_longer_expands_proportionally_to_its_target(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5 REPAIRED: a 500 MB-target
        bomb used to expand proportionally to its (attacker-chosen) target --
        peak RSS scaled with `target_decompressed`, not with the ceiling.
        Now it is bounded by `segment._MAX_RESIDUE_DECODED_BYTES` regardless
        of how large the attacker's target claims to be: this asserts peak
        RSS stays within a small, fixed multiple of that ceiling (some
        overhead is expected -- `text`/`lines`/parsed-record copies of the
        decoded bytes -- but it must not scale with the 500 MB target), and
        that the read finishes well inside a healthy deadline instead of
        needing a 60s SIGKILL to even find out."""
        from app.realtime import segment as sg

        target_decompressed = 500_000_000       # 500 MB
        seg_dir, compressed_bytes = build_residue_segment(
            tmp_path, ENV, "bomb-segment", target_decompressed)
        events_path = seg_dir / "events.jsonl.gz"

        assert compressed_bytes < 5_000_000, (
            f"expected the compressed residue to be a few MB at most (this "
            f"is the whole point: SMALL on disk, LARGE decompressed) -- "
            f"got {compressed_bytes} bytes")

        script = measurement_script(str(REPO_ROOT), str(events_path))
        # A healthy, BOUNDED read should finish in a few seconds, not need
        # anywhere close to the old 60s "documenting an unbounded read"
        # deadline -- kept generous (20s) for a slow/loaded host.
        verdict = run_with_parent_timeout(script, timeout_s=20.0,
                                          repo_root=str(REPO_ROOT))
        assert verdict.classification == "COMPLETED", (
            f"expected the now-bounded decompression to complete well "
            f"within 20s -- got {verdict}")
        result = json.loads(verdict.stdout.strip().splitlines()[-1])

        rss_bytes = result["ru_maxrss"] * _RU_MAXRSS_UNIT
        amplification = rss_bytes / compressed_bytes

        print(f"\n[A5] {compressed_bytes} bytes on disk -> "
              f"{result['elapsed_s']:.2f}s wall time, "
              f"{rss_bytes / 1e6:.1f} MB peak RSS "
              f"({amplification:.0f}x amplification), reading "
              f"{result['n_records_read']} record(s) -- bounded by the "
              f"{sg._MAX_RESIDUE_DECODED_BYTES / 1e6:.0f} MB decoded-byte "
              "ceiling, not by the attacker's 500 MB target.")

        # THE REPAIRED PROPERTY: peak RSS is a small, FIXED multiple of the
        # decoded-byte ceiling, not of the 500 MB target -- generous
        # headroom (10x) for the text/lines/parsed-record copies
        # `read_segment_records` makes of the decoded bytes.
        assert rss_bytes < 10 * sg._MAX_RESIDUE_DECODED_BYTES, (
            f"expected peak RSS to be bounded by a small multiple of the "
            f"{sg._MAX_RESIDUE_DECODED_BYTES / 1e6:.0f} MB decoded-byte "
            f"ceiling -- got {rss_bytes / 1e6:.1f} MB, which looks "
            "proportional to the 500 MB target again")
        assert result["elapsed_s"] < 15.0, (
            f"expected a bounded read to finish in a few seconds -- got "
            f"{result['elapsed_s']:.2f}s")

    def test_reading_the_bomb_in_process_sets_the_capped_flag(self, tmp_path):
        """A smaller (in-process-safe) bomb, read directly rather than out
        of process, so `read_segment_records.capped` -- the diagnostic A6's
        residue classification depends on to distinguish "cut off by the
        ceiling" from an ordinary torn/malformed stream -- can be asserted
        directly."""
        from app.realtime import segment as sg

        seg_dir, compressed_bytes = build_residue_segment(
            tmp_path, ENV, "bomb-segment-inprocess", 200_000_000)  # 200 MB
        records = sg.read_segment_records(seg_dir / "events.jsonl.gz")
        assert sg.read_segment_records.capped is True, (
            "expected a 200MB-target bomb (well over the decoded-byte "
            "ceiling) to set the capped diagnostic")
        assert len(records) > 0, (
            "the readable prefix up to the cap should still yield records")

    def test_an_ordinary_small_segment_is_never_capped(self, tmp_path):
        """Negative control: a genuinely small, well-formed segment must
        NOT be reported as capped -- the ceiling must not misclassify
        ordinary evidence as having hit a limit it never approached."""
        from app.realtime import archive_head as ah
        from app.realtime import canonical as cn
        from app.realtime import segment as sg
        from datetime import datetime, timedelta, timezone

        root = tmp_path / "archive"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg-ordinary",
                             partition_identity="p", commit_to_head=False)
        now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
        for i in range(20):
            w.submit({
                "connection_generation": 1, "subscription_id": 4,
                "subscription_generation": 1, "message_type": "orderbook_delta",
                "market_ticker": "KXA", "seq": i,
                "received_at_utc": cn.canonical_datetime(
                    now + timedelta(microseconds=i)),
                "received_monotonic_ns": 1_000_000 + i,
                "raw_event": {"price_dollars": "0.5100", "side": "no"},
                "normalized_event": {"raw_price_units": 5100},
            })
        import time
        deadline_iterations = 0
        while w.accounting.written < 20:
            deadline_iterations += 1
            assert deadline_iterations < 2000, "writer thread never finished"
            time.sleep(0.005)
        w.close()
        records = sg.read_segment_records(w.events_path)
        assert len(records) == 20
        assert sg.read_segment_records.capped is False

    def test_the_bomb_is_reachable_via_verify_archives_own_residue_scan(
            self, tmp_path):
        """Confirms the ATTACK SURFACE claim directly: `verify_archive`
        itself -- not merely `read_segment_records` called in isolation --
        walks into this residue directory and decompresses it, with no
        manifest, no writer lock, and no ownership check standing in the
        way. Uses a small (fast, in-process-safe) bomb so this specific
        test stays quick; the out-of-process test above is what measures
        the actual amplification."""
        from app.realtime import archive_head as ah
        from app.realtime import segment as sg

        root = tmp_path / "archive"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        build_residue_segment(root, ENV, "small-bomb", 5_000_000)  # 5MB

        result = sg.verify_archive(root, environment=ENV)
        # It was reached and decompressed (records_read reflects it via
        # `uncommitted_records_present`/`uncommitted_segment_detail`) --
        # not refused, not skipped, not size-limited before decompression.
        detail = result["uncommitted_segment_detail"]
        assert any(d["segment_id"] == "small-bomb" for d in detail), (
            f"expected verify_archive's own residue scan to have examined "
            f"the planted segment directory: {detail}")
        matched = next(d for d in detail if d["segment_id"] == "small-bomb")
        assert matched["records_read"] > 0, (
            "verify_archive's residue scan should have decompressed and "
            f"parsed at least one record from the bomb: {matched}")
