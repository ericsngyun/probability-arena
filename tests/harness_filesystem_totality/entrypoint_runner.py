#!/usr/bin/env python3
"""Subprocess dispatcher for one (entry point, root) cell of the matrix.

Invoked as a CHILD PROCESS, never imported in-process, because the whole
point of this harness is that a hang inside the production code (a blocking
`open()` on a FIFO, an unbounded `read_bytes()` on `/dev/zero`) must be
killable from OUTSIDE the interpreter that is stuck. An in-process deadline
(threading.Timer, signal.alarm, asyncio.wait_for) cannot do that: the call
that hangs is a syscall, so control never returns to Python to notice the
deadline, and `app/realtime/archive_head.py::_read_json`'s
`except Exception` would in any case convert an in-process SIGALRM
`TimeoutError` into a clean `ArchiveHeadError` -- a false PASS.

Reads one JSON object from argv[1] describing the call to make, makes it,
and prints EXACTLY ONE JSON line to stdout describing the outcome. This
script must never raise past its own top-level `except BaseException` --
if it does, that is itself evidence of a defect (an uncaught traceback),
and the caller records it as such rather than losing it to a non-zero exit
code with no explanation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _call(args: dict):
    """Return (verdict_kind, detail) for one entry point. May raise."""
    entrypoint = args["entrypoint"]
    root = args["root"]
    environment = args.get("environment", "demo")

    if entrypoint == "verify_segment":
        from app.realtime import segment as sg
        v = sg.verify_segment(Path(args["segment_dir"]), environment=environment,
                              root=Path(root))
        return "RETURNED", {"valid": v.valid, "state": str(v.state),
                            "reasons": v.reasons}

    if entrypoint == "verify_archive":
        from app.realtime import segment as sg
        report = sg.verify_archive(Path(root), environment=environment)
        return "RETURNED", {"verdict": report["verdict"],
                            "head_state": report.get("head_state"),
                            "reasons": report.get("reasons"),
                            "orphaned_committed_segments":
                                report.get("orphaned_committed_segments"),
                            "warnings": report.get("warnings")}

    if entrypoint == "head_state":
        from app.realtime import archive_head as ah
        state = ah.head_state(Path(root), environment)
        return "RETURNED", {"state": state.get("state"),
                            "reason": state.get("reason")}

    if entrypoint == "load_authoritative_head":
        from app.realtime import archive_head as ah
        head = ah.load_authoritative_head(Path(root), environment)
        return "RETURNED", {"generation": head.generation}

    if entrypoint == "recover_current_head":
        from app.realtime import archive_head as ah
        rec = ah.recover_current_head(Path(root), environment)
        return "RETURNED", {"generation": rec.get("generation")}

    if entrypoint == "archive_verify":
        from app.realtime import archive as ar
        a = ar.EventArchive(Path(root), environment=environment)
        result = a.verify()
        return "RETURNED", {"verdict": result["verdict"],
                            "intact": result["intact"],
                            "reasons": result.get("reasons")}

    if entrypoint == "archive_read_verified":
        from app.realtime import archive as ar
        a = ar.EventArchive(Path(root), environment=environment)
        records = a.read_verified()
        return "RETURNED", {"record_count": len(records)}

    if entrypoint == "archive_read_unverified_diagnostic":
        from app.realtime import archive as ar
        a = ar.EventArchive(Path(root), environment=environment)
        records = a.read_unverified_diagnostic()
        return "RETURNED", {"record_count": len(records),
                            "diagnostic_order_unauthenticated":
                                a.diagnostic_order_unauthenticated,
                            "missing_committed_segments":
                                a.missing_committed_segments}

    if entrypoint == "archive_append":
        from app.realtime import archive as ar
        from app.realtime import book as bk
        from datetime import datetime, timezone
        a = ar.EventArchive(Path(root), environment=environment)
        envelope = bk.make_envelope(
            venue="kalshi", environment=environment, channel="orderbook_delta",
            message={"type": "orderbook_delta", "sid": 1, "seq": 999,
                    "msg": {"market_ticker": "KXPROBE", "market_id": "mid-x",
                           "yes_dollars_fp": [], "no_dollars_fp": []}},
            receive_time=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
            receive_mono=bk.monotonic_ns())
        a.append(envelope)
        return "RETURNED", {"written": a.written}

    if entrypoint == "scan_legacy":
        from app.realtime import legacy_import as li
        scan = li.scan_legacy(args["source"])
        return "RETURNED", {"records_readable": scan.records_readable,
                            "torn_files": scan.torn_files,
                            "undecodable_files": scan.undecodable_files}

    if entrypoint == "migrate_legacy_dry_run":
        from app.realtime import legacy_import as li
        plan = li.migrate_legacy_archive(
            args["source"], args["dest"], environment=environment,
            confirm=False)
        return "RETURNED", {"would_import": plan.get("would_import")}

    raise ValueError(f"unknown entrypoint {entrypoint!r}")


def main() -> int:
    raw = sys.argv[1]
    args = json.loads(raw)
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
