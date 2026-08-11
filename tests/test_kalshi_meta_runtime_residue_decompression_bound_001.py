"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A5 -- residue decompression bound.

Reproduces the reviewer's finding: a small, realistic, gzip-compressed
"residue" events file -- exactly what `mkdir` plus a dropped file gives
anyone with write access to the archive root, no manifest and no writer
lock required -- expands, uncapped, to hundreds of megabytes in RAM when
`segment.read_segment_records` (reached via `verify_archive`'s own
residue-inspection loop) decompresses it. Measured OUT OF PROCESS, with a
PARENT-ENFORCED timeout, so a much larger bomb than this file actually
constructs (the reviewer measured a 2 GiB expansion SIGKILLed at 120s)
could never hang this suite even if attempted.

DOES NOT MODIFY `app/realtime/segment.py` or `app/realtime/evidence_fs.py`.
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


class TestDecompressionAmplificationIsUnbounded:
    def test_a_small_compressed_residue_expands_far_beyond_its_own_size(
            self, tmp_path):
        target_decompressed = 500_000_000       # 500 MB
        seg_dir, compressed_bytes = build_residue_segment(
            tmp_path, ENV, "bomb-segment", target_decompressed)
        events_path = seg_dir / "events.jsonl.gz"

        assert compressed_bytes < 5_000_000, (
            f"expected the compressed residue to be a few MB at most (this "
            f"is the whole point: SMALL on disk, LARGE decompressed) -- "
            f"got {compressed_bytes} bytes")

        script = measurement_script(str(REPO_ROOT), str(events_path))
        # Generous parent deadline: this is measuring an EXISTING lack of a
        # bound, not racing a real one -- 60s is far more than a healthy,
        # correctly-bounded read of a few-MB file should ever need, and
        # comfortably covers a 500MB decompression on a slow host without
        # the parent-enforced kill masking the measurement itself.
        verdict = run_with_parent_timeout(script, timeout_s=60.0,
                                          repo_root=str(REPO_ROOT))
        assert verdict.classification == "COMPLETED", (
            f"the decompression either crashed or hit the PARENT's 60s "
            f"deadline (and was SIGKILLed) -- itself notable (an unbounded "
            f"decompression that a reasonable deadline cannot even "
            f"complete), but this assertion documents which outcome "
            f"actually happened: {verdict}")
        result = json.loads(verdict.stdout.strip().splitlines()[-1])

        rss_bytes = result["ru_maxrss"] * _RU_MAXRSS_UNIT
        amplification = rss_bytes / compressed_bytes

        print(f"\n[A5] {compressed_bytes} bytes on disk -> "
              f"{result['elapsed_s']:.2f}s wall time, "
              f"{rss_bytes / 1e6:.1f} MB peak RSS "
              f"({amplification:.0f}x amplification), reading "
              f"{result['n_records_read']} well-formed record(s) -- no cap "
              "refused this, no exception was raised, no output-size limit "
              "exists on this path today.")

        # THE STILL-PRESENT DEFECT: production enforces no cap here at
        # all -- the read COMPLETES, uncapped, proportional to the
        # decompressed size, not the compressed size on disk.
        assert amplification > 50, (
            f"expected a large (>50x) amplification, demonstrating no "
            f"output cap exists -- got {amplification:.1f}x. If this is "
            "now small, either the bomb generator's compression ratio "
            "degraded (unrelated to production) or a real bound was added "
            "(in which case this test should be rewritten to assert "
            "REFUSAL, not measure an amplification ratio)")
        assert rss_bytes > 100_000_000, (
            f"expected >100MB peak RSS for a 500MB-target bomb; got "
            f"{rss_bytes / 1e6:.1f}MB -- either the platform RSS unit "
            "normalisation is wrong for this host or the decompression "
            "did not actually run to completion")

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
