"""A3 fixtures: a healthy segment (via the PRODUCTION write path, reused
read-only from `tests/harness_filesystem_totality/archive_fixture.py`) plus
one additional shape that harness does not build: a symlink at the events
file pointing at a real FIFO -- the shape the milestone names as the
`verify_segment(root=None)` hang.

Only `build_healthy_archive`/`make_short_root`/`teardown_root` are imported
from `tests.harness_filesystem_totality.archive_fixture` -- read-only reuse
of a fixture builder, never a write to that module or any file under
`tests/harness_filesystem_totality/`.
"""

from __future__ import annotations

import os
from pathlib import Path


def build_healthy_segment(root: Path, *, environment: str = "demo") -> dict:
    from tests.harness_filesystem_totality.archive_fixture import build_healthy_archive
    return build_healthy_archive(root, environment=environment)


def poison_events_path_with_fifo_symlink(layout: dict) -> Path:
    """Replace the healthy segment's events file with a symlink to a real
    FIFO. Returns the FIFO path (never opened by this function -- opening it
    for read/write is exactly the operation under test).

    A FIFO directly AT the events path is caught by `evidence_fs.presence`
    (`os.lstat` sees `S_ISFIFO`, not REG/DIR/LNK) before any open is
    attempted, regardless of `root`. A symlink WHOSE TARGET is a FIFO is
    different: `os.lstat` on the symlink itself reports `S_ISLNK`, which
    `presence` treats as "present" without following it -- so whether the
    reader ever opens through that symlink depends entirely on whether a
    LATER stage still gates on it, which is exactly the `root`-argument
    question A3 exists to answer empirically.
    """
    events_path = layout["events_path"]
    side_dir = layout["root"].parent / f"{layout['root'].name}-fifo-side"
    side_dir.mkdir(parents=True, exist_ok=True)
    fifo_path = side_dir / "events.fifo"
    if fifo_path.exists():
        fifo_path.unlink()
    os.mkfifo(fifo_path)
    events_path.unlink()
    os.symlink(fifo_path, events_path)
    return fifo_path


def cleanup_fifo_side(layout: dict) -> None:
    side_dir = layout["root"].parent / f"{layout['root'].name}-fifo-side"
    import shutil
    shutil.rmtree(side_dir, ignore_errors=True)
