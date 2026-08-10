"""A3 — the public argument-shape matrix for `verify_segment`, the entry
point A1's call graph already proved reaches a raw `open()` two hops down
(`verify_segment -> file_sha256 -> open(...)`), and whose `root=None` shape
the milestone names explicitly as unexercised (`grep -rn "root=None" tests/`
returned nothing before this module existed).

Dimensions covered, matching A3's required list:

  * all-defaults              -- `root_mode="omit"`, `allow_open` omitted
  * None for an optional      -- `root_mode="none"` (root=None IS the
                                  supported-but-unexercised optional shape)
  * explicit root              -- `root_mode="explicit_correct"`
  * implicit/derived root      -- `root_mode="omit"` (the `_DERIVE_ROOT`
                                  sentinel default)
  * path alias                 -- `root_mode="explicit_wrong"` (a
                                  syntactically different but semantically
                                  equivalent-looking root the caller could
                                  plausibly pass)
  * empty values                -- `environment=""`
  * diagnostic vs canonical     -- `allow_open=True` (the one flag that
                                  changes verify_segment's mode from
                                  strict-committed to permit-open-segment)
  * missing optional metadata   -- exercised at the FIXTURE level (the
                                  poisoned segment's manifest/venue metadata
                                  is whatever `build_healthy_archive` wrote;
                                  see `fixtures.py`)

`FIXTURES` names which on-disk shape a cell runs against: `"healthy"` (a
committed, valid segment -- every cell here is expected to return a bounded
verdict FAST) or `"poisoned_fifo_symlink"` (the events file replaced with a
symlink to a real FIFO -- this is where a `root` shape that skips the
containment-and-symlink check is expected to hang).
"""

from __future__ import annotations

import itertools

ROOT_MODES = ("omit", "explicit_correct", "explicit_wrong", "none")
ALLOW_OPEN_VALUES = (False, True)
ENVIRONMENTS = ("demo", "", "production")


def build_healthy_matrix() -> list[dict]:
    """Every (root_mode, allow_open, environment) cell against a genuinely
    valid, committed segment. None of these should ever hang -- the matrix
    is here to prove that every SUPPORTED shape returns a bounded, typed
    result (either `SegmentVerdict` or a typed exception), never a raw
    builtin exception and never a hang."""
    cells = []
    for root_mode, allow_open, environment in itertools.product(
            ROOT_MODES, ALLOW_OPEN_VALUES, ENVIRONMENTS):
        cells.append({
            "api": "verify_segment", "fixture": "healthy",
            "root_mode": root_mode, "allow_open": allow_open,
            "environment": environment,
            "label": f"root={root_mode},allow_open={allow_open},"
                     f"environment={environment!r}",
        })
    return cells


def build_poisoned_matrix() -> list[dict]:
    """Every `root_mode` against the symlink-to-FIFO fixture, at the
    CORRECT environment and default `allow_open` -- this isolates `root` as
    the single variable under test for the milestone's named defect. A short
    parent-enforced timeout is mandatory for these cells; see
    `parent_timeout.py`."""
    cells = []
    for root_mode in ROOT_MODES:
        cells.append({
            "api": "verify_segment", "fixture": "poisoned_fifo_symlink",
            "root_mode": root_mode, "allow_open": False,
            "environment": "demo",
            "label": f"root={root_mode} against a symlink-to-FIFO events path",
        })
    return cells


def build_full_matrix() -> list[dict]:
    return build_healthy_matrix() + build_poisoned_matrix()
