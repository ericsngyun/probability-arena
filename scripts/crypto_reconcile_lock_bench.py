#!/usr/bin/env python3
"""
crypto_reconcile_lock_bench.py — DISPOSABLE, throwaway benchmark harness for
the crypto tape reconciler's SQLite write-coordination claims
(CRYPTO-COVERAGE-REPAIR-001).

============================================================================
DO NOT RUN THIS AGAINST PRODUCTION. DO NOT POINT DATABASE_URL AT A REAL DB.
============================================================================

This script exists because NEW-HIGH-1 and NEW-HIGH-4 (third Lane-B review,
SQLite coexistence) both survived several review rounds for the SAME
underlying reason: every write-coordination figure in this codebase
(RECONCILE_BATCH_SIZE's safe hold duration, RECONCILE_POST_BATCH_YIELD_
SECONDS's claimed 10-40x wait reduction) was session-only, ad-hoc,
non-reproducible benchmark evidence. A mutation battery can prove the
CONSTANTS are pinned; it proves nothing about whether they are CORRECT on
any given host. This script is the harness that should have existed before
those numbers were written down.

It is intentionally NOT part of the pytest suite (it takes real wall-clock
seconds, spawns a real competing process, and its results are host-speed
dependent — the opposite of what a deterministic, fast CI test should be).
It is NOT imported by any application code. It is a disposable tool: copy
it, point --db-path at a scratch file, read the numbers, throw the copy
away.

Usage (always against a throwaway --db-path, never a real one):

    .venv/bin/python scripts/crypto_reconcile_lock_bench.py \\
        --db-path /tmp/lock-bench-scratch.db \\
        --seed-tokens 200 \\
        --batch-size 5 \\
        --busy-timeout-ms 30000 \\
        --competitor-hold-ms 50 \\
        --trials 3

What it measures, per trial:
  - the reconciler pass's own wall time and batches_committed/status
  - the competing writer's observed wait distribution (min/p50/p95/max)
  - the competing writer's total successful write count during the pass
    (throughput)
  - duty cycle: fraction of the pass's wall time the reconciler itself held
    the write lock (approximated from blocked_ms on the reconciler side)

Mechanism: a real second PROCESS (not thread — avoids GIL/connection-pool
artifacts) repeatedly attempts `BEGIN IMMEDIATE; UPDATE ...; COMMIT;`
against the SAME sqlite file in a tight loop for the duration of the
reconciler pass, recording each attempt's wait-to-acquire time. This is the
same "real second connection, real BEGIN IMMEDIATE" shape the regression
tests in tests/test_crypto_coverage_repair_001.py use, just sustained for
the whole pass instead of a single injected lock.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _competitor_process(
    db_path: str, busy_timeout_ms: int, stop_event: "mp.Event", waits: "mp.Queue"
) -> None:
    """Runs in a SEPARATE process. Tight loop of real write transactions
    against the same file, recording each attempt's wait-to-acquire time
    (perf_counter delta between attempting BEGIN IMMEDIATE and it
    succeeding). Reports the full list of observed waits (seconds) and the
    total successful-write count via the queue when done.

    Stops on `stop_event` (graceful, checked between attempts), NOT on a
    hard process kill — `proc.terminate()` (SIGTERM) would kill this before
    it ever reaches `waits.put(...)` at the end, silently discarding every
    observation the whole harness exists to collect."""
    conn = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000.0)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    local_waits: list[float] = []
    successes = 0
    while not stop_event.is_set():
        t0 = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE crypto_tokens SET last_seen_at = last_seen_at "
                "WHERE id = (SELECT id FROM crypto_tokens LIMIT 1)"
            )
            conn.commit()
            local_waits.append(time.perf_counter() - t0)
            successes += 1
        except sqlite3.OperationalError:
            # busy_timeout exhausted this attempt; count the wait anyway,
            # it is still real time spent trying.
            local_waits.append(time.perf_counter() - t0)
            try:
                conn.rollback()
            except sqlite3.OperationalError:
                pass
    conn.close()
    waits.put((local_waits, successes))


def _seed(db_path: str, n_tokens: int) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.models import CryptoToken

    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    now = datetime.now(timezone.utc)
    for i in range(n_tokens):
        session.add(CryptoToken(
            chain="solana",
            token_address=f"bench-tok-{i:06d}",
            symbol=f"BENCH{i}",
            first_seen_at=now - timedelta(hours=30),
            last_seen_at=now - timedelta(hours=30),
        ))
    session.commit()
    session.close()
    engine.dispose()


def _run_one_trial(
    db_path: str, batch_size: int, busy_timeout_ms: int,
    competitor_hold_ms: int, yield_seconds: float,
) -> dict:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.services.crypto_tape import CryptoLifecycleTapeRecorder, CryptoTapeConfig

    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"timeout": busy_timeout_ms / 1000.0},
    )
    Session = sessionmaker(bind=engine)
    session = Session()

    waits_q: mp.Queue = mp.Queue()
    stop_event = mp.Event()
    proc = mp.Process(
        target=_competitor_process,
        args=(db_path, busy_timeout_ms, stop_event, waits_q),
    )
    proc.start()
    time.sleep(0.05)  # let the competitor get moving before the pass starts

    rec = CryptoLifecycleTapeRecorder(CryptoTapeConfig(chain="solana"))
    pass_t0 = time.perf_counter()
    result = rec.run_once(
        session, limit=100000, hours=48, batch_size=batch_size,
        oldest_first=True, exclude_final=True,
        sleeper=(lambda s: time.sleep(s)) if yield_seconds else (lambda s: None),
    )
    pass_wall = time.perf_counter() - pass_t0

    # stop the competitor GRACEFULLY (it must reach `waits.put(...)`) —
    # never proc.terminate(), which would SIGTERM-kill it before that.
    stop_event.set()
    local_waits, competitor_successes = ([], 0)
    try:
        local_waits, competitor_successes = waits_q.get(timeout=10)
    except Exception:
        pass
    proc.join(timeout=10)
    if proc.is_alive():
        proc.terminate()  # last-resort cleanup only, after we already have data

    session.close()
    engine.dispose()

    waits_sorted = sorted(local_waits) if local_waits else [0.0]
    p50 = statistics.median(waits_sorted)
    p95 = waits_sorted[int(len(waits_sorted) * 0.95)] if len(waits_sorted) > 1 else waits_sorted[0]

    return {
        "status": result.get("status"),
        "batches_committed": result.get("batches_committed"),
        "tokens_processed": result.get("tokens_processed"),
        "blocked_ms": result.get("blocked_ms"),
        "pass_wall_seconds": round(pass_wall, 3),
        "competitor_min_wait": round(min(waits_sorted), 4),
        "competitor_p50_wait": round(p50, 4),
        "competitor_p95_wait": round(p95, 4),
        "competitor_max_wait": round(max(waits_sorted), 4),
        "competitor_successful_writes": competitor_successes,
        "duty_cycle": (
            round((result.get("blocked_ms") or 0) / 1000.0 / pass_wall, 3)
            if pass_wall > 0 else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", required=True,
        help="THROWAWAY sqlite file path. Refuses any path that looks like "
             "a real deployment path (contains 'probability-arena' outside "
             "/tmp, or matches common production filenames).",
    )
    parser.add_argument("--seed-tokens", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--busy-timeout-ms", type=int, default=30000)
    parser.add_argument("--competitor-hold-ms", type=int, default=50)
    parser.add_argument("--yield-seconds", type=float, default=0.05)
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()

    db_path = args.db_path
    forbidden_markers = ("probability_arena", "/mnt/data", "arena.db")
    if any(m in db_path for m in forbidden_markers) and "/tmp" not in db_path:
        print(
            f"REFUSING: --db-path {db_path!r} looks like it could be a real "
            "deployment path. This tool is DISPOSABLE and must only ever "
            "run against a throwaway scratch file (e.g. under /tmp).",
            file=sys.stderr,
        )
        return 2

    if Path(db_path).exists():
        print(f"REFUSING: {db_path} already exists — use a fresh scratch path.")
        return 2

    print(f"Seeding {args.seed_tokens} tokens into {db_path} ...")
    _seed(db_path, args.seed_tokens)

    results = []
    for trial in range(1, args.trials + 1):
        # each trial needs a fresh set of non-final tokens; re-seed between
        # trials into a new scratch file to avoid state bleed from the
        # previous trial's writes/finalization.
        trial_db = f"{db_path}.trial{trial}"
        _seed(trial_db, args.seed_tokens)
        print(f"\n--- trial {trial}/{args.trials} ---")
        r = _run_one_trial(
            trial_db, args.batch_size, args.busy_timeout_ms,
            args.competitor_hold_ms, args.yield_seconds,
        )
        for k, v in r.items():
            print(f"  {k}: {v}")
        results.append(r)
        os.remove(trial_db)

    print("\n=== summary across trials ===")
    max_waits = [r["competitor_max_wait"] for r in results]
    p95_waits = [r["competitor_p95_wait"] for r in results]
    writes = [r["competitor_successful_writes"] for r in results]
    duty = [r["duty_cycle"] for r in results if r["duty_cycle"] is not None]
    print(f"  max_wait range: {min(max_waits):.3f}s - {max(max_waits):.3f}s")
    print(f"  p95_wait range: {min(p95_waits):.3f}s - {max(p95_waits):.3f}s")
    print(f"  competitor_writes range: {min(writes)} - {max(writes)}")
    if duty:
        print(f"  duty_cycle range: {min(duty):.3f} - {max(duty):.3f}")

    Path(db_path).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
