#!/usr/bin/env python3
"""
crypto_reconcile_coexistence_bench.py — DISPOSABLE competing-writer bench for
the crypto tape reconciler, before vs after `ANALYZE`
(CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001, B3).

============================================================================
DO NOT RUN THIS AGAINST PRODUCTION. DO NOT POINT --work-db AT A REAL DB.
============================================================================

Why this exists, and why it is not `crypto_reconcile_lock_bench.py`
-------------------------------------------------------------------
The older harness runs its competitor in a TIGHT LOOP — as many
`BEGIN IMMEDIATE; UPDATE; COMMIT` cycles per second as one process can issue.
That competitor is not a model of EVO. On EVO the only always-on writer is the
watcher (~1 write burst per 60 s) and the 5-minute MarketOps cycle. A
100 %-duty-cycle competitor measures a machine nobody runs, and it was
explicitly acknowledged as unrepresentative. It is retained here as a
SEPARATELY LABELLED adversarial mode, never as the capacity number.

The primary mode is a **uniform-arrival estimator**. A realistic writer arrives
at an instant that is essentially uncorrelated with the reconciler's lock
cycle, so the wait it experiences is a draw from the wait distribution of a
uniformly-random arrival during the pass. Sampling that distribution once per
60 s would give ~0 samples in a 20 s pass — no statistical power at all — so
the probe writer instead issues ONE real write transaction per `--probe-period`
seconds (default 1.0 s) on a FIXED ABSOLUTE SCHEDULE with a random initial
phase. The absolute schedule is the part that makes it an estimator rather than
a renewal process: sleeping a fresh interval *after* each transaction would
couple arrivals to the system, under-sampling exactly the intervals where the
reconciler holds the lock longest and biasing the measured wait downward. At
~1 ms of work per second the probe's own duty cycle is ~0.1 %, so it samples
the system without materially perturbing it — which is exactly the property the
tight-loop competitor lacks.

A `paced` mode is also provided which fires at the literal EVO cadences
(watcher 60 s, MarketOps 300 s) for face validity. It is honest but low-n; it
confirms, it does not carry the argument.

What is measured, per trial, all DIRECTLY (nothing is inferred from anything
else — this milestone has already been burned once by inferring writer wait
from lock hold, and they are different metrics):

  * reconciliation pass wall time, tokens processed, status
  * the reconciler's own write-transaction hold p50/p95/max (first write
    statement to commit return, via SQLAlchemy cursor hooks + a Session.commit
    wrapper)
  * the competing writer's wait-to-acquire p50/p95/max, measured as the
    perf_counter delta across `BEGIN IMMEDIATE` in the OTHER process
  * `database is locked` failures and retry counts on the competing side
  * the competing writer's successful-write count

Each trial restores a pristine copy first, because a reconciliation pass
mutates the database.

Usage (always against throwaway copies, never a real deployment DB):

    .venv/bin/python scripts/crypto_reconcile_coexistence_bench.py \\
        --pristine-db /scratch/pristine.db --work-db /scratch/coexist.db \\
        --trials 3 --mode probe --out /scratch/coexist_before.json
    ... same with --analyze for the after arm.

Not part of the pytest suite (real wall-clock seconds, two real processes,
host-speed dependent); imported by no application code.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

_DEPLOYMENT_MARKERS = ("projects/probability-arena/data", "/var/lib/", "/srv/")

_PRODUCTION_BASENAMES = ("probability_arena.db",)


def _refuse_non_scratch(path) -> None:
    """Refuse anything that looks like a real deployment OR development
    database.

    A three-substring blacklist tuned to one host is not a guard: a
    developer's own `<repo>/data/probability_arena.db` matches none of those
    fragments, and this script WRITES. The filename check is what actually
    stops that case; the repo-root check stops a scratch copy landing inside a
    checkout."""
    from pathlib import Path as _P
    s = str(path)
    for frag in _DEPLOYMENT_MARKERS:
        if frag in s:
            raise SystemExit(f"refusing a deployment-looking path: {path}")
    if _P(s).name in _PRODUCTION_BASENAMES:
        raise SystemExit(
            f"refusing {_P(s).name!r}: that is the production database "
            "filename. Copy it to a scratch name first."
        )
    repo_root = _P(__file__).resolve().parents[1]
    if repo_root in _P(s).resolve().parents:
        raise SystemExit(f"refusing a path inside the repo checkout: {path}")


def _pctl(values, p):
    if not values:
        return None
    o = sorted(values)
    return o[min(len(o) - 1, max(0, int(round(p * (len(o) - 1)))))]


# --- the competing writer, in a REAL separate process -------------------------
def _competitor(db_path: str, mode: str, busy_timeout_ms: int, probe_period: float,
                stop: "mp.Event", out: "mp.Queue") -> None:
    """One real second connection. Writes the shape the watcher writes: a
    single `market_price_ticks` INSERT inside an explicit immediate
    transaction. Records the wait-to-acquire for EVERY attempt, successful or
    not, plus lock failures and retries.

    Stops on `stop` (graceful) so it always reaches `out.put(...)` — a
    SIGTERM here would discard the entire measurement.
    """
    conn = sqlite3.connect(db_path, timeout=busy_timeout_ms / 1000.0,
                           isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    waits: list[float] = []
    txn_totals: list[float] = []
    ok = 0
    locked = 0
    retries = 0
    seq = 0
    # paced mode fires at the literal EVO cadences. The initial phase is
    # RANDOM: firing at t=0 of every trial would sample one fixed point of the
    # reconciler's lock cycle over and over and call the result a
    # distribution.
    t_start = time.perf_counter()
    paced_next = {"watcher": t_start + random.random() * 60.0,
                  "marketops": t_start + random.random() * 300.0}
    # probe mode: an absolute schedule with a random initial phase, so the
    # first arrival is not pinned to t=0 of every trial either.
    probe_next = [t_start + random.random() * probe_period]

    while not stop.is_set():
        now = time.perf_counter()
        if mode == "adversarial":
            pass                      # fire immediately, no pacing at all
        elif mode == "paced":
            due = min(paced_next.values())
            if now < due:
                time.sleep(min(0.05, due - now))
                continue
            for k, period in (("watcher", 60.0), ("marketops", 300.0)):
                if now >= paced_next[k]:
                    paced_next[k] = now + period
        else:                          # "probe" — uniform-arrival estimator
            # FIXED ABSOLUTE SCHEDULE, not sleep-after-work. Sleeping for a
            # fresh interval after each transaction couples the arrival process
            # to the system being measured: a transaction that waits a long
            # time pushes the next arrival back, so exactly the intervals where
            # the reconciler holds the lock longest get UNDER-sampled and the
            # measured wait is biased downward. Advancing an absolute deadline
            # keeps the arrival times independent of what the reconciler is
            # doing; a late attempt catches up instead of shifting the schedule.
            if now < probe_next[0]:
                time.sleep(min(0.02, probe_next[0] - now))
                continue
            while probe_next[0] <= now:
                probe_next[0] += probe_period

        seq += 1
        t0 = time.perf_counter()
        acquired = None
        attempt = 0
        while True:
            attempt += 1
            try:
                conn.execute("BEGIN IMMEDIATE")
                acquired = time.perf_counter()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                locked += 1
                if attempt >= 2 or stop.is_set():
                    break
                retries += 1
        if acquired is None:
            waits.append(time.perf_counter() - t0)
            continue
        waits.append(acquired - t0)
        try:
            conn.execute(
                "INSERT INTO market_price_ticks "
                "(market_ticker, observed_at, yes_bid, yes_ask, midpoint, "
                " spread, volume_24h, liquidity_proxy, created_at) "
                "VALUES (?, datetime('now'), 40, 42, 0.41, 2, 0, 0, "
                " datetime('now'))",
                (f"COEXIST-BENCH-{seq}",),
            )
            conn.execute("COMMIT")
            ok += 1
            txn_totals.append(time.perf_counter() - t0)
        except sqlite3.OperationalError:
            locked += 1
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
    conn.close()
    out.put({
        "mode": mode,
        "attempts": seq,
        "successful_writes": ok,
        "lock_failures": locked,
        "retries": retries,
        "wall_s": time.perf_counter() - t_start,
        "waits": waits,
        "txn_totals": txn_totals,
    })


# --- the reconciliation pass, in THIS process --------------------------------
def _run_pass(db_path: str, batch_size: int, max_duration: float,
              busy_timeout_ms: int) -> dict:
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.config import get_settings
    from app.db import get_engine, get_sessionmaker
    from app.services import crypto_tape as ct

    engine = get_engine()
    open_txn: list[float | None] = [None]
    holds: list[float] = []
    stack: list[dict] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        stack.append({"t0": time.perf_counter(),
                      "in_txn": bool(conn.connection.driver_connection.in_transaction)})

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        c = stack.pop()
        after = bool(conn.connection.driver_connection.in_transaction)
        if not c["in_txn"] and after and open_txn[0] is None:
            open_txn[0] = c["t0"]      # this statement took the RESERVED lock

    original_commit = Session.commit

    def _commit(self, *a, **kw):
        try:
            return original_commit(self, *a, **kw)
        finally:
            if open_txn[0] is not None:
                holds.append(time.perf_counter() - open_txn[0])
                open_txn[0] = None

    Session.commit = _commit

    settings = get_settings()
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    session = get_sessionmaker()()
    t0 = time.perf_counter()
    # `Session.commit` is patched GLOBALLY. If `run_once` raises — which it
    # does in every adversarial trial — restoring it in the happy path only
    # would leave the patch installed, leak the session and engine, and make
    # the NEXT trial capture the already-patched function as its "original",
    # nesting wrappers and keeping the previous trial's `holds` alive through
    # the closure. try/finally, always.
    try:
        summary = rec.run_once(
            session, limit=2000, hours=48, dry_run=False,
            oldest_first=True, include_backlog=True, exclude_final=True,
            min_age_minutes=ct.SHORTEST_HORIZON_CLOSING_EDGE_MINUTES,
            run_config_extra={"mode": "scheduled_reconciliation", "forced": True,
                              "bench": "coexistence"},
            skip_redundant_when_final=True,
            batch_size=batch_size, max_duration_seconds=max_duration,
            sleeper=time.sleep,
        )
    finally:
        Session.commit = original_commit
        try:
            session.close()
        except Exception:
            pass
        engine.dispose()
    wall = time.perf_counter() - t0
    return {
        "status": summary.get("status"),
        "tokens_processed": summary.get("tokens_processed"),
        "batches_committed": summary.get("batches_committed"),
        "blocked_ms": summary.get("blocked_ms"),
        "pass_wall_s": round(wall, 3),
        "hold_n": len(holds),
        "hold_p50": _pctl(holds, 0.5),
        "hold_p95": _pctl(holds, 0.95),
        "hold_max": max(holds) if holds else None,
        "hold_sum": sum(holds),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pristine-db", required=True,
                    help="untouched copy restored before every trial")
    ap.add_argument("--work-db", required=True, help="throwaway working copy")
    ap.add_argument("--out", required=True)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--mode", choices=("probe", "paced", "adversarial"),
                    default="probe")
    ap.add_argument("--analyze", action="store_true",
                    help="ANALYZE the restored copy before each pass")
    ap.add_argument("--drop-stats", action="store_true",
                    help="DELETE every sqlite_stat1 row on the restored copy "
                         "before each pass, reconstructing the no-statistics "
                         "planner state. Needed once ANALYZE has been run on "
                         "the source database: from that point on, no copy of "
                         "production is un-analysed, so the 'before' arm can "
                         "only be reconstructed, not found. SQLite treats an "
                         "empty sqlite_stat1 exactly as it treats a missing "
                         "one — verified in crypto_query_plan_mechanism.py's "
                         "V0 variant, which reproduces the un-analysed plans "
                         "and latencies on an analysed file.")
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--max-duration-seconds", type=float, default=20.0)
    ap.add_argument("--busy-timeout-ms", type=int, default=30000)
    ap.add_argument("--probe-period", type=float, default=1.0)
    args = ap.parse_args()

    # BOTH paths, not just --work-db: --pristine-db is read every trial and a
    # typo pointing it at the live file would read production 12 times under a
    # write-saturating competitor.
    for p in (args.work_db, args.pristine_db):
        _refuse_non_scratch(Path(p).resolve())
    if not Path(args.pristine_db).exists():
        raise SystemExit(f"no such pristine copy: {args.pristine_db}")

    trials = []
    for t in range(1, args.trials + 1):
        # a crashed previous trial can leave a rollback journal next to the
        # working copy; restoring the file without removing it would hand the
        # next trial a "hot journal" to replay.
        for suffix in ("-journal", "-wal", "-shm"):
            Path(args.work_db + suffix).unlink(missing_ok=True)
        shutil.copyfile(args.pristine_db, args.work_db)
        analyze_s = None
        stats_rows = None
        if args.analyze or args.drop_stats:
            c = sqlite3.connect(args.work_db)
            if args.drop_stats:
                try:
                    c.execute("DELETE FROM sqlite_stat1")
                    c.commit()
                except sqlite3.Error:
                    pass          # no sqlite_stat1 at all is the same state
            if args.analyze:
                a0 = time.perf_counter()
                c.execute("ANALYZE")
                c.commit()
                analyze_s = round(time.perf_counter() - a0, 3)
            try:
                stats_rows = c.execute(
                    "SELECT count(*) FROM sqlite_stat1").fetchone()[0]
            except sqlite3.Error:
                stats_rows = 0
            c.close()

        q: mp.Queue = mp.Queue()
        stop = mp.Event()
        proc = mp.Process(target=_competitor,
                          args=(args.work_db, args.mode, args.busy_timeout_ms,
                                args.probe_period, stop, q))
        # daemon: if this parent dies unexpectedly the competitor must not
        # outlive it holding the write lock forever.
        proc.daemon = True
        proc.start()
        time.sleep(1.0)                     # let the competitor settle

        # A failing pass is a RESULT, not a crash. In `adversarial` mode the
        # reconciler genuinely cannot complete — that is the finding — and the
        # `finally` is what guarantees the competitor is still stopped and
        # drained, so the trial is recorded instead of hanging at interpreter
        # exit waiting on a child that was never told to stop.
        try:
            res = _run_pass(args.work_db, args.batch_size,
                            args.max_duration_seconds, args.busy_timeout_ms)
        except Exception as exc:
            res = {"status": "pass_failed", "error": f"{type(exc).__name__}: {exc}"[:400],
                   "tokens_processed": None, "batches_committed": None,
                   "pass_wall_s": None, "hold_n": 0, "hold_p50": None,
                   "hold_p95": None, "hold_max": None, "hold_sum": None}
        finally:
            stop.set()
        comp = {}
        try:
            comp = q.get(timeout=30)
        except Exception:
            comp = {"error": "competitor produced no result"}
        proc.join(timeout=30)
        if proc.is_alive():
            proc.terminate()

        waits = comp.pop("waits", [])
        txn = comp.pop("txn_totals", [])
        trial = {
            "trial": t,
            "analyze_seconds": analyze_s,
            "sqlite_stat1_rows_at_pass_start": stats_rows,
            "pass": res,
            "competitor": {
                **comp,
                "wait_p50": _pctl(waits, 0.5), "wait_p95": _pctl(waits, 0.95),
                "wait_max": max(waits) if waits else None,
                "wait_n": len(waits),
                "txn_total_p95": _pctl(txn, 0.95),
                "txn_total_max": max(txn) if txn else None,
            },
        }
        trials.append(trial)
        print(json.dumps(trial, indent=2, default=str))

    agg_wait95 = [t["competitor"]["wait_p95"] for t in trials
                  if t["competitor"].get("wait_p95") is not None]
    agg_waitmax = [t["competitor"]["wait_max"] for t in trials
                   if t["competitor"].get("wait_max") is not None]
    report = {
        "mode": args.mode,
        "analyzed": args.analyze,
        "stats_dropped": args.drop_stats,
        "trials": trials,
        "aggregate": {
            "tokens_processed": [t["pass"]["tokens_processed"] for t in trials],
            "pass_wall_s": [t["pass"]["pass_wall_s"] for t in trials],
            "hold_p95": [t["pass"]["hold_p95"] for t in trials],
            "hold_max": [t["pass"]["hold_max"] for t in trials],
            "competitor_wait_p95_range": (
                [min(agg_wait95), max(agg_wait95)] if agg_wait95 else None),
            "competitor_wait_max": max(agg_waitmax) if agg_waitmax else None,
            "lock_failures_total": sum(
                t["competitor"].get("lock_failures", 0) for t in trials),
            "retries_total": sum(
                t["competitor"].get("retries", 0) for t in trials),
        },
    }
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print("\n=== aggregate ===")
    print(json.dumps(report["aggregate"], indent=2, default=str))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
