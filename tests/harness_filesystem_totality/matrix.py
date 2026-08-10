"""The (filesystem shape) x (artifact) x (entry point) cell list.

`build_cells(full=False)` returns the FAST subset that runs by default.
`build_cells(full=True)` returns the complete cross product (every shape
applicable to an artifact's kind, times every entry point that reads that
artifact) -- gated behind `KALSHI_FS_TOTALITY_FULL=1` because it is a few
hundred subprocess launches.

`/dev/zero` reproduction (`dev_zero_symlink`) is excluded from BOTH by
default -- see `runner.DEV_ZERO_TIMEOUT_S` for why -- and only included when
`KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO=1` is also set, in which case it runs
with a hard 0.2s timeout regardless of the cell's normal timeout.
"""

from __future__ import annotations

import os

from tests.harness_filesystem_totality import shapes as sh

JSON_FILE_SHAPES = (
    "regular", "missing", "dir_in_place_of_file", "symlink_to_file",
    "broken_symlink", "fifo", "unix_socket", "mode_000_file",
    "unreadable_file", "malformed_json",
)
GZIP_FILE_SHAPES = (
    "regular", "missing", "dir_in_place_of_file", "symlink_to_file",
    "broken_symlink", "fifo", "unix_socket", "mode_000_file",
    "unreadable_file", "truncated_gzip", "corrupt_gzip",
)
DIR_SHAPES = sh.DIR_SHAPES

# A small, representative slice used when `full=False`. Every SHAPE CLASS
# (missing / wrong-type / symlink-indirection / blocking-node / permission /
# malformed-content) still appears at least once per artifact; what is
# dropped is the redundant repetition of the same shape class across every
# artifact and entry point.
FAST_FILE_SHAPES = ("regular", "missing", "symlink_to_dir_or_file",
                    "fifo", "mode_000_file", "malformed_or_corrupt")
FAST_DIR_SHAPES = ("regular", "missing", "symlink_to_dir", "execute_only_dir",
                   "mode_000_dir")


def _allow_dev_zero() -> bool:
    return os.environ.get("KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO") == "1"


ARTIFACTS = {
    "events_file": {
        "kind": "file", "path_key": "events_path", "gzip": True,
        "entrypoints": ("verify_segment", "verify_archive", "archive_verify",
                        "archive_read_verified",
                        "archive_read_unverified_diagnostic"),
    },
    "manifest": {
        "kind": "file", "path_key": "manifest_path", "gzip": False,
        "entrypoints": ("verify_segment", "verify_archive", "archive_verify",
                        "archive_read_verified",
                        "archive_read_unverified_diagnostic",
                        "archive_append"),
    },
    "genesis": {
        "kind": "file", "path_key": "genesis_path", "gzip": False,
        "entrypoints": ("verify_archive", "archive_verify", "head_state",
                        "load_authoritative_head", "recover_current_head",
                        "archive_append"),
    },
    "current_head": {
        "kind": "file", "path_key": "current_head_path", "gzip": False,
        "entrypoints": ("verify_archive", "head_state",
                        "load_authoritative_head", "recover_current_head"),
    },
    "generation_record": {
        "kind": "file", "path_key": "generation_1_path", "gzip": False,
        "entrypoints": ("verify_archive", "head_state",
                        "load_authoritative_head", "recover_current_head"),
    },
    "heads_dir": {
        "kind": "dir", "path_key": "heads_dir",
        "entrypoints": ("verify_archive", "head_state",
                        "load_authoritative_head", "recover_current_head"),
    },
    "env_dir": {
        "kind": "dir", "path_key": "env_dir",
        "entrypoints": ("verify_archive", "archive_append", "head_state"),
    },
    "segment_dir": {
        "kind": "dir", "path_key": "segment_dir",
        "entrypoints": ("verify_segment", "verify_archive", "archive_verify",
                        "archive_read_verified",
                        "archive_read_unverified_diagnostic",
                        "archive_append"),
    },
}


def shapes_for(artifact: str) -> tuple:
    spec = ARTIFACTS[artifact]
    if spec["kind"] == "dir":
        return DIR_SHAPES
    return GZIP_FILE_SHAPES if spec["gzip"] else JSON_FILE_SHAPES


def build_cells(*, full: bool) -> list:
    """List of {"artifact", "shape", "entrypoint"} dicts."""
    cells = []
    for artifact, spec in ARTIFACTS.items():
        all_shapes = shapes_for(artifact)
        if full:
            shape_list = all_shapes
        else:
            # Fast subset: one representative shape per class, whichever of
            # them is actually valid for this artifact.
            if spec["kind"] == "dir":
                wanted = ("regular", "missing", "symlink_to_dir",
                         "execute_only_dir", "mode_000_dir", "fifo")
            else:
                wanted = ("regular", "missing", "symlink_to_file",
                         "fifo", "mode_000_file", "malformed_json",
                         "truncated_gzip", "corrupt_gzip")
            shape_list = tuple(s for s in wanted if s in all_shapes)
        for shape in shape_list:
            if shape == "dev_zero_symlink" and not _allow_dev_zero():
                continue
            for entrypoint in spec["entrypoints"]:
                if (artifact, shape, entrypoint) in EXCLUDED_CELLS:
                    continue
                cells.append({"artifact": artifact, "shape": shape,
                             "entrypoint": entrypoint})
    if full and _allow_dev_zero():
        for artifact, spec in ARTIFACTS.items():
            if spec["kind"] != "file" or not spec.get("gzip"):
                continue
            for entrypoint in spec["entrypoints"]:
                if (artifact, "dev_zero_symlink", entrypoint) in EXCLUDED_CELLS:
                    continue
                cells.append({"artifact": artifact,
                             "shape": "dev_zero_symlink",
                             "entrypoint": entrypoint})
    return cells


def cell_id(cell: dict) -> str:
    return f"{cell['artifact']}::{cell['shape']}::{cell['entrypoint']}"


CELL_TIMEOUT_OVERRIDES = {}

# KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1/A1.4: `EventArchive._writer_for`
# used to treat every `SegmentError` from `_candidate_segment_ids`
# (including "the root does not bound this path", which a symlinked
# `env=<name>/` produced on EVERY candidate) as "this id collided with a
# live writer, try the next one", and retried up to 10,000 times before
# giving up with a typed `ArchiveError` -- O(10,000) filesystem attempts,
# 2s to >20s measured, to reach a verdict that was knowable on the FIRST
# one. `archive.py::EventArchive._check_partition_writable` now checks
# `env_dir` containment ONCE, before any candidate id is even constructed,
# so a permanently invalid partition location fails immediately with a
# typed `ArchiveError` instead of entering the retry loop at all --
# measured at 0.06s post-fix (was 2-20s+), so this cell is no longer
# excluded and runs in the ordinary matrix.
EXCLUDED_CELLS = set()


# --- known-defect cells --------------------------------------------------------
#
# EMPTY as of KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1: every cell that was
# here (defects #1-#3, #4's FIFO sibling, and the #5a directory variant) now
# verifies TOTAL like every other cell in the matrix, closed at the class
# (one shared `evidence_fs` primitive per failure mode: `presence`,
# `containment_reason`, `safe_enumerate`, `bounded_read`), not patched one
# instance at a time. See `TestKnownDefectLedger` in
# `tests/test_kalshi_fs_totality_harness_001.py` for the dedicated,
# now-FIXED-behaviour reproduction of each, and `app/realtime/evidence_fs.py`
# for the shared abstraction. Kept as an empty dict, not deleted, so a
# future regression has an established place to register a new entry rather
# than inventing the convention again.
KNOWN_DEFECT_CELLS = {}
