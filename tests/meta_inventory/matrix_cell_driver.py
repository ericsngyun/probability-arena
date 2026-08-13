#!/usr/bin/env python3
"""A3 child-process dispatcher: make ONE call with ONE argument shape and
report the outcome as a single JSON line. Never imported in-process -- the
whole reason A3 runs cells in a subprocess is that `verify_segment(root=
None)` against a symlinked FIFO can hang inside a blocking `open()`, and an
in-process deadline cannot interrupt a blocked syscall (see `tests/harness_
filesystem_totality/entrypoint_runner.py`'s docstring, which established
this reasoning first; this script is independent test infrastructure that
applies the same reasoning to argument SHAPES rather than filesystem
SHAPES).

Must never raise past its own top-level `except BaseException` -- an
uncaught traceback here is itself an A3 finding (a raw builtin exception
escaping a supported public call), not a harness bug to hide.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


_MISSING = object()


def _call(args: dict):
    api = args["api"]
    kwargs = dict(args.get("kwargs") or {})

    if api == "verify_segment":
        from app.realtime import segment as sg
        directory = Path(kwargs.pop("directory"))
        root_mode = kwargs.pop("root_mode", "omit")
        if root_mode == "omit":
            pass                                  # use the function default
        elif root_mode == "none":
            kwargs["root"] = None
        elif root_mode.startswith("explicit"):
            kwargs["root"] = Path(kwargs.pop("root_value"))
        v = sg.verify_segment(directory, **kwargs)
        return "RETURNED", {"valid": v.valid, "state": str(v.state),
                            "reasons": v.reasons}

    if api == "verify_archive":
        from app.realtime import segment as sg
        root = Path(kwargs.pop("root"))
        v = sg.verify_archive(root, **kwargs)
        return "RETURNED", {"verdict": v.get("verdict"),
                            "reasons": v.get("reasons")}

    if api == "read_genesis":
        from app.realtime import archive_head as ah
        root = Path(kwargs.pop("root"))
        environment = kwargs.pop("environment")
        g = ah.read_genesis(root, environment, **kwargs)
        return "RETURNED", {"archive_id": g.get("archive_id")}

    if api == "head_state":
        from app.realtime import archive_head as ah
        root = Path(kwargs.pop("root"))
        environment = kwargs.pop("environment")
        s = ah.head_state(root, environment, **kwargs)
        return "RETURNED", {"state": s.get("state")}

    raise ValueError(f"unknown api {api!r}")


def main() -> int:
    args = json.loads(sys.argv[1])
    result = {"raised": False, "exception_type": None,
              "exception_message": None, "outcome": None, "detail": None}
    try:
        kind, detail = _call(args)
        result["outcome"] = kind
        result["detail"] = detail
    except BaseException as exc:            # noqa: BLE001 - reported, not hidden
        result["raised"] = True
        result["exception_type"] = type(exc).__name__
        result["exception_module"] = type(exc).__module__
        result["exception_message"] = str(exc)[:2000]
    sys.stdout.write(json.dumps(result) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
