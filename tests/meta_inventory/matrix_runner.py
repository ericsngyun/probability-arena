"""A3 — glue: build a fixture, translate one matrix cell into
`matrix_cell_driver.py` arguments, run it under `parent_timeout`, and
classify. Kept separate from `argument_matrix.py` (data) and
`matrix_cell_driver.py` (child process) so each piece is independently
readable.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.meta_inventory import fixtures as fx
from tests.meta_inventory import parent_timeout as pt

_DRIVER = Path(__file__).with_name("matrix_cell_driver.py")

# Healthy cells never touch a blocking primitive; generous but still bounded.
HEALTHY_TIMEOUT_S = 5.0
# Poisoned cells are the ones this matrix exists to prove hang or don't --
# short enough that a real hang costs the suite a few seconds, not minutes.
POISONED_TIMEOUT_S = 3.0


def _driver_args(cell: dict, layout: dict) -> dict:
    directory = str(layout["segment_dir"])
    kwargs = {"directory": directory, "environment": cell["environment"],
              "allow_open": cell["allow_open"], "root_mode": cell["root_mode"]}
    if cell["root_mode"] == "explicit_correct":
        kwargs["root_value"] = str(layout["root"])
    elif cell["root_mode"] == "explicit_wrong":
        # A syntactically different but plausible-looking root: the
        # segment's OWN directory instead of the archive root two levels up
        # -- exactly the off-by-one a caller reading `verify_segment`'s
        # `<root>/env=<name>/segment=<id>` layout comment could make.
        kwargs["root_value"] = str(layout["segment_dir"])
    return {"api": cell["api"], "kwargs": kwargs}


def run_cell(cell: dict, layout: dict) -> dict:
    args = _driver_args(cell, layout)
    timeout_s = (POISONED_TIMEOUT_S if cell["fixture"] == "poisoned_fifo_symlink"
                else HEALTHY_TIMEOUT_S)
    outcome = pt.run_python_with_parent_timeout(
        str(_DRIVER), json.dumps(args), timeout_s=timeout_s)
    outcome["cell"] = cell
    outcome["timeout_s"] = timeout_s
    if outcome["classification"] == "EXITED_CLEAN":
        lines = [ln for ln in outcome["stdout"].splitlines() if ln.strip()]
        if len(lines) != 1:
            outcome["classification"] = "PROTOCOL_VIOLATION"
        else:
            try:
                payload = json.loads(lines[0])
            except json.JSONDecodeError:
                outcome["classification"] = "PROTOCOL_VIOLATION"
            else:
                if payload.get("raised"):
                    outcome["classification"] = "RAISED"
                    outcome["exception_type"] = payload.get("exception_type")
                    outcome["exception_message"] = payload.get("exception_message")
                else:
                    outcome["classification"] = "RETURNED"
                    outcome["detail"] = payload.get("detail")
    return outcome


def build_layout(root: Path, *, environment: str = "demo", poisoned: bool) -> dict:
    layout = fx.build_healthy_segment(root, environment=environment)
    if poisoned:
        fx.poison_events_path_with_fifo_symlink(layout)
    return layout
