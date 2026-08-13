"""Glue: build a fresh healthy archive, mutate ONE artifact into ONE shape,
run ONE entry point against it in a subprocess, and classify the result.
"""

from __future__ import annotations

from pathlib import Path

from tests.harness_filesystem_totality import archive_fixture as af
from tests.harness_filesystem_totality import shapes as sh
from tests.harness_filesystem_totality.matrix import ARTIFACTS, CELL_TIMEOUT_OVERRIDES
from tests.harness_filesystem_totality.runner import (
    DEFAULT_TIMEOUT_S, DEV_ZERO_TIMEOUT_S, run_cell,
)


def apply_shape(layout: dict, artifact: str, shape: str) -> Path:
    """Mutate `layout[artifact]`'s canonical path into `shape`. Returns the path."""
    spec = ARTIFACTS[artifact]
    target = layout[spec["path_key"]]
    side_dir = layout["root"].parent / f"{layout['root'].name}-side"
    if spec["kind"] == "file":
        original = target.read_bytes() if target.exists() else b"{}"
        sh.place_file_shape(target, shape, original_bytes=original,
                            side_dir=side_dir)
    else:
        # A template copy of the directory as it was BEFORE mutation, so
        # `symlink_to_dir` / `execute_only_dir` still resolve to real,
        # otherwise-healthy content.
        template_holder = side_dir / f"{target.name}.template"
        if template_holder.exists():
            import shutil
            shutil.rmtree(template_holder)
        if target.exists() and not target.is_symlink():
            import shutil
            shutil.copytree(target, template_holder)
        sh.place_dir_shape(target, shape, template=template_holder,
                           side_dir=side_dir)
    return target


def run_matrix_cell(cell: dict, *, environment: str = "demo") -> dict:
    """Build, mutate, run, classify. Returns the raw `runner.run_cell` dict
    plus `cell` for context. Caller applies `acceptance.classify_cell`."""
    root = af.make_short_root()
    try:
        layout = af.build_healthy_archive(root, environment=environment)
        apply_shape(layout, cell["artifact"], cell["shape"])

        override_key = (cell["artifact"], cell["shape"], cell["entrypoint"])
        if cell["shape"] == "dev_zero_symlink":
            timeout_s = DEV_ZERO_TIMEOUT_S
        else:
            timeout_s = CELL_TIMEOUT_OVERRIDES.get(override_key,
                                                    DEFAULT_TIMEOUT_S)
        kwargs = {"timeout_s": timeout_s}
        if cell["entrypoint"] == "verify_segment":
            kwargs["segment_dir"] = layout["segment_dir"]

        result = run_cell(cell["entrypoint"], root=root, environment=environment,
                          **kwargs)
        result["cell"] = cell
        return result
    finally:
        af.teardown_root(root)
        af.teardown_root(root.parent / f"{root.name}-side")
