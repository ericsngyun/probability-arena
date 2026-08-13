#!/usr/bin/env python3
"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- the rotation-cost benchmark,
committed as a runnable script so it can be REGRESSED rather than re-invented.

WHY THIS EXISTS AS A FILE. `DEFAULT_MAX_SEGMENT_RECORDS` was derived from an
ad hoc benchmark that was never committed ("reproducible by submitting N
records to a fresh `SegmentWriter` and timing `close()`"), and it was run
with `commit_to_head=False`. `SegmentWriter` DEFAULTS that flag to True and
`EventArchive._writer_for` never overrides it, so production's close does
strictly more work than the number in the source comment describes: after
reconciliation it runs a full independent `verify_segment` (a THIRD complete
read of the segment) and then `commit_segment`, which fsyncs a generation
record, the current-head pointer and their directories under the archive
lock. Benchmarking the cheaper of the two shapes and shipping the bound
derived from it is how a ~3 s stall got documented as ~1.4 s.

USAGE

    python tests/benchmarks/segment_close_cost.py                 # default sweep
    python tests/benchmarks/segment_close_cost.py --records 20000 --repeat 3
    python tests/benchmarks/segment_close_cost.py --json out.json

Reports, per record count and per `commit_to_head` setting: append
throughput, close WALL time, close CPU time, and the implied close latency
per 1,000 records. Wall and CPU are reported separately on purpose -- the
gap between them is the part a loaded host inflates, and it is the reason
the bound is set from the CPU figure with headroom rather than from a
best-case wall figure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.realtime import archive_head as ah        # noqa: E402
from app.realtime import canonical as cn           # noqa: E402
from app.realtime import segment as sg             # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
ENV = "demo"


def fields(i: int) -> dict:
    """The real Kalshi orderbook-delta envelope shape, not a toy payload."""
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def one_trial(*, n: int, commit_to_head: bool) -> dict:
    root = Path(tempfile.mkdtemp(prefix="segclose-"))
    try:
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        w = sg.SegmentWriter(root, environment=ENV, segment_id="seg",
                             partition_identity="p",
                             commit_to_head=commit_to_head)
        t0, c0 = time.perf_counter(), time.process_time()
        for i in range(n):
            assert w.submit(fields(i)) is None
        append_wall = time.perf_counter() - t0
        append_cpu = time.process_time() - c0

        t1, c1 = time.perf_counter(), time.process_time()
        manifest = w.close()
        close_wall = time.perf_counter() - t1
        close_cpu = time.process_time() - c1
        assert manifest["record_count"] == n
        return {
            "records": n,
            "commit_to_head": commit_to_head,
            "append_wall_s": append_wall,
            "append_cpu_s": append_cpu,
            "append_ev_per_s": n / append_wall if append_wall else None,
            "close_wall_s": close_wall,
            "close_cpu_s": close_cpu,
            "close_wall_ms_per_1000": 1000.0 * close_wall / n * 1000 / 1000,
            "close_cpu_ms_per_1000": 1000.0 * close_cpu / n * 1000 / 1000,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, nargs="*",
                    default=[1000, 5000, 13000, 20000])
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--commit-to-head", choices=["true", "false", "both"],
                    default="both")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    modes = ({"true": [True], "false": [False], "both": [False, True]}
             [args.commit_to_head])
    rows = []
    print(f"{'records':>8} {'cth':>6} {'append ev/s':>12} {'close wall':>11} "
          f"{'close cpu':>10} {'wall ms/1k':>11} {'cpu ms/1k':>10}")
    for n in args.records:
        for cth in modes:
            for _ in range(args.repeat):
                r = one_trial(n=n, commit_to_head=cth)
                rows.append(r)
                print(f"{r['records']:>8} {str(cth):>6} "
                      f"{r['append_ev_per_s']:>12.0f} "
                      f"{r['close_wall_s']:>10.3f}s "
                      f"{r['close_cpu_s']:>9.3f}s "
                      f"{1000 * r['close_wall_s'] / n * 1000:>11.1f} "
                      f"{1000 * r['close_cpu_s'] / n * 1000:>10.1f}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
