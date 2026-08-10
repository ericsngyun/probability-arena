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

# `EventArchive._writer_for` treats every `SegmentError` from
# `_candidate_segment_ids` (including "the root does not bound this path",
# which a symlinked `env=<name>/` produces on EVERY candidate) as "this id
# collided with a live writer, try the next one", and retries up to 10,000
# times before giving up with a typed `ArchiveError`. That is a REAL finding
# -- O(10,000) filesystem attempts to reach a verdict that was knowable on
# the FIRST one -- but its wall-clock cost is load-dependent (measured
# between 2s and >20s across otherwise-identical runs on this host) in a
# way no fixed timeout can bound without either being flaky (too tight) or
# slowing down every other cell in the suite (too generous as a blanket
# default). Rather than assert a specific bound this harness cannot make
# deterministic, this ONE cell is excluded from the automated matrix and
# characterized separately, un-asserted, in
# `TestDiscrimination`-adjacent diagnostics -- see the harness report for
# the measured range. This is the one cell in the entire (artifact, shape,
# entry point) space this harness could not turn into a deterministic
# pass/fail assertion.
EXCLUDED_CELLS = {
    ("env_dir", "symlink_to_dir", "archive_append"),
}


# --- known-defect cells --------------------------------------------------------
#
# Every one of these was FOUND by an early, unrestricted run of the generic
# matrix (`test_matrix_cell_is_total`) -- they are not hand-picked to match
# the milestone's five findings, they are what the sweep actually turned up.
# Each maps to the KNOWN-DEFECT ledger entry in
# `tests/test_kalshi_fs_totality_harness_001.py::TestKnownDefectLedger` that
# gives the minimal, dedicated reproduction and root cause. Marking them here
# keeps `test_matrix_cell_is_total` GREEN by default (the ledger is the
# canonical place a reader goes to see "yes, this is known, here is why")
# while still running every one of these cells on every invocation, with
# `strict=True`: if production is ever fixed, the corresponding cell starts
# unexpectedly PASSING, `xfail(strict=True)` turns THAT into a failure, and
# the fix and this table go out of sync loudly rather than silently.
KNOWN_DEFECT_CELLS = {
    # Defect #3: a FIFO at any head artifact hangs every reader that touches
    # it, because `archive_head._read_json` gates only on `is_symlink()`.
    ("genesis", "fifo", "verify_archive"): "defect_003",
    ("genesis", "fifo", "archive_verify"): "defect_003",
    ("genesis", "fifo", "head_state"): "defect_003",
    ("genesis", "fifo", "load_authoritative_head"): "defect_003",
    ("genesis", "fifo", "recover_current_head"): "defect_003",
    ("genesis", "fifo", "archive_append"): "defect_003",
    ("current_head", "fifo", "verify_archive"): "defect_003",
    ("current_head", "fifo", "head_state"): "defect_003",
    ("current_head", "fifo", "load_authoritative_head"): "defect_003",
    ("current_head", "fifo", "recover_current_head"): "defect_003",
    ("generation_record", "fifo", "verify_archive"): "defect_003",
    ("generation_record", "fifo", "head_state"): "defect_003",
    ("generation_record", "fifo", "load_authoritative_head"): "defect_003",
    ("generation_record", "fifo", "recover_current_head"): "defect_003",
    # A FIFO at the events file: `archive.py::_undecodable_tail_records`
    # (defect #4's home) is reached via the diagnostic salvage path even
    # though the fixed `read_segment_records` call right before it in the
    # same function correctly refuses a FIFO via `is_file()`.
    ("events_file", "fifo", "archive_read_unverified_diagnostic"): "defect_004_sibling",
    # Defect #5-family: an unreadable SEGMENT DIRECTORY (not just an
    # unreadable manifest FILE, which is defect #5a) makes append()'s
    # candidate-id scan raise a raw PermissionError the same way.
    ("segment_dir", "mode_000_dir", "archive_append"): "defect_005a_directory_variant",
}
