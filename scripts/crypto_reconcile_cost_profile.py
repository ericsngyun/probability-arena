#!/usr/bin/env python3
"""
crypto_reconcile_cost_profile.py — DISPOSABLE, throwaway cost-decomposition
harness for the crypto tape reconciler (CRYPTO-RECONCILIATION-CAPACITY-001 B1).

============================================================================
DO NOT RUN THIS AGAINST PRODUCTION. DO NOT POINT --db-path AT A REAL DB.
============================================================================

Companion to `scripts/crypto_reconcile_lock_bench.py`, which answers "how long
does the write lock get held and what does a competing writer wait?". This one
answers the DIFFERENT question CRYPTO-COVERAGE-REPAIR-001's fifth round left
open: **where does the per-token cost actually go**, so capacity can be raised
without weakening the 2.0s write-time SLO.

It runs the REAL `CryptoLifecycleTapeRecorder.run_once` with exactly the kwargs
`run_scheduled_reconciliation` passes on the scheduled path, so the shape being
measured is the shipped shape. It changes NO application behaviour: every piece
of instrumentation is monkeypatched in THIS process only —

  * SQLAlchemy `before_cursor_execute` / `after_cursor_execute` engine hooks
    give per-statement wall time, CPU time, rowcount, and — critically — whether
    a write transaction was already open when the statement ran;
  * `perf_counter` wrappers around the recorder's own methods
    (`_load_sources`, `build_birth_event`, `build_snapshot`,
    `build_actor_observation`, `compute_survival`, the selection queries)
    attribute wall/CPU to a named component;
  * a wrapper on `Session.commit` closes each write-transaction window, so
    every recorded interval can be split into the part INSIDE the write lock
    and the part OUTSIDE it.

That inside/outside split is the whole point. Two different limiters exist and
they can have different answers:

  (a) what limits TOKENS PER TRANSACTION — i.e. what consumes the
      `RECONCILE_WRITE_TIME_SLO_SECONDS` write-lock budget; and
  (b) what limits TOKENS PER DAY — i.e. total pass wall time, including
      everything outside the transaction.

Usage (always against a throwaway copy, never a real deployment DB):

    # make the copy with the same online-backup API app/services/backup.py uses
    python - <<'EOF'
    import sqlite3
    src = sqlite3.connect("file:/path/to/live.db?mode=ro", uri=True)
    dst = sqlite3.connect("/scratch/copy.db")
    with dst:
        src.backup(dst)
    EOF

    .venv/bin/python scripts/crypto_reconcile_cost_profile.py \\
        --db-path /scratch/copy.db --label rep1 --out-dir /scratch

Reruns are only comparable against a FRESH copy: a pass mutates the database
(that is what it is for), so restore the copy between repetitions.

It is intentionally NOT part of the pytest suite (it takes real wall-clock
seconds and its results are host-speed dependent) and is NOT imported by any
application code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

_DEPLOYMENT_MARKERS = ("projects/probability-arena/data", "/var/lib/", "/srv/")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db-path", required=True,
                   help="throwaway SQLite copy to profile (NEVER a real DB)")
    p.add_argument("--label", default="run", help="tag for the output files")
    p.add_argument("--out-dir", default=".", help="where to write the JSON")
    p.add_argument("--limit", type=int, default=2000,
                   help="selection limit (crypto_tape_reconciler_limit)")
    p.add_argument("--batch-size", type=int, default=5,
                   help="RECONCILE_BATCH_SIZE")
    p.add_argument("--max-duration-seconds", type=float, default=20.0,
                   help="RECONCILE_MAX_DURATION_SECONDS")
    p.add_argument("--window-hours", type=int, default=48)
    p.add_argument("--i-understand-this-writes", action="store_true",
                   help="acknowledge that the profiled pass mutates --db-path")
    return p.parse_args()


ARGS = _parse_args()
DB_PATH = Path(ARGS.db_path).resolve()
for _frag in _DEPLOYMENT_MARKERS:
    if _frag in str(DB_PATH):
        raise SystemExit(
            f"refusing to profile a deployment-looking path: {DB_PATH}\n"
            "This harness WRITES. Point it at a throwaway copy."
        )
if not DB_PATH.exists():
    raise SystemExit(f"no such database: {DB_PATH}")
if not ARGS.i_understand_this_writes:
    print(f"NOTE: the profiled pass WRITES to {DB_PATH} (it is a real "
          f"reconciliation pass). Re-run with --i-understand-this-writes.",
          file=sys.stderr)
    raise SystemExit(2)

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import event  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.services import crypto_tape as ct  # noqa: E402

# --- recording ---------------------------------------------------------------
EVENTS: list[dict] = []
TOKEN_ROWS: list[dict] = []
TXN_WINDOWS: list[list[float]] = []
STATE = {"token_idx": -1, "batch_idx": -1, "phase": ["pass"]}
_open_txn: list[float | None] = [None]
_sql_stack: list[dict] = []

SOURCE_TABLES = (
    "crypto_pairs", "crypto_price_ticks", "crypto_token_risk_assessments",
    "crypto_token_discovery_events", "meme_attention_snapshots",
    "meme_catalyst_events",
)
TABLES = SOURCE_TABLES + (
    "crypto_tokens", "crypto_token_birth_events",
    "crypto_token_survival_outcomes", "crypto_token_lifecycle_snapshots",
    "crypto_token_actor_observations", "crypto_token_lifecycle_runs",
)


def _emit(kind: str, label: str, t0: float, t1: float, cpu: float, **extra) -> None:
    EVENTS.append({
        "kind": kind, "label": label, "t0": t0, "t1": t1, "wall": t1 - t0,
        "cpu": cpu, "token_idx": STATE["token_idx"],
        "batch_idx": STATE["batch_idx"], "phase": STATE["phase"][-1], **extra,
    })


def classify_sql(sql: str) -> tuple[str, str, str]:
    """(component, io_kind, table) from the statement text alone."""
    low = " ".join(sql.split()).lower()
    verb = low.split()[0] if low else ""
    io = "read" if verb == "select" else (
        "write" if verb in ("insert", "update", "delete", "replace") else "other")
    tbl = next((t for t in TABLES if t in low), "?")
    if io == "write":
        comp = {
            "crypto_token_birth_events": "birth_write",
            "crypto_token_survival_outcomes": "outcome_write",
            "crypto_token_lifecycle_snapshots": "snapshot_write",
            "crypto_token_actor_observations": "actor_write",
            "crypto_token_lifecycle_runs": "txn_overhead",
        }.get(tbl, f"other_write:{tbl}")
    elif io == "read":
        if tbl in SOURCE_TABLES:
            comp = "source_row_load"
        elif tbl == "crypto_tokens":
            comp = "selection"
        elif tbl == "crypto_token_survival_outcomes":
            comp = "outcome_write"       # the read half of the outcome upsert
        else:
            comp = "txn_overhead"
    else:
        comp = "txn_overhead"
    return comp, io, tbl


engine = get_engine()


@event.listens_for(engine, "before_cursor_execute")
def _before_cursor(conn, cursor, statement, parameters, context, executemany):
    _sql_stack.append({
        "t0": time.perf_counter(), "c0": time.process_time(),
        "in_txn": bool(conn.connection.driver_connection.in_transaction),
    })


@event.listens_for(engine, "after_cursor_execute")
def _after_cursor(conn, cursor, statement, parameters, context, executemany):
    ctx = _sql_stack.pop()
    t1, c1 = time.perf_counter(), time.process_time()
    in_after = bool(conn.connection.driver_connection.in_transaction)
    if not ctx["in_txn"] and in_after and _open_txn[0] is None:
        _open_txn[0] = ctx["t0"]        # this statement took the write lock
    comp, io, tbl = classify_sql(statement)
    _emit("sql", comp, ctx["t0"], t1, c1 - ctx["c0"], io=io, table=tbl,
          verb=" ".join(statement.split())[:12].upper(),
          in_txn_before=ctx["in_txn"], in_txn_after=in_after,
          rowcount=(cursor.rowcount if cursor.rowcount is not None else -1))


def _timed_method(cls, name: str, label: str):
    original = getattr(cls, name)

    def wrapper(*a, **kw):
        STATE["phase"].append(label)
        t0, c0 = time.perf_counter(), time.process_time()
        try:
            return original(*a, **kw)
        finally:
            STATE["phase"].pop()
            _emit("method", label, t0, time.perf_counter(),
                  time.process_time() - c0)
    setattr(cls, name, wrapper)


R = ct.CryptoLifecycleTapeRecorder
_original_load_sources = R._load_sources


def _load_sources(self, session, token, now):
    STATE["token_idx"] += 1
    STATE["phase"].append("source_row_load")
    t0, c0 = time.perf_counter(), time.process_time()
    sources = None
    try:
        sources = _original_load_sources(self, session, token, now)
        return sources
    finally:
        STATE["phase"].pop()
        rows = {
            "pairs": len(sources.pairs) if sources else 0,
            "ticks": len(sources.ticks) if sources else 0,
            "assessments": len(sources.assessments) if sources else 0,
            "discovery_events": len(sources.discovery_events) if sources else 0,
            "attention": 1 if (sources and sources.attention is not None) else 0,
        }
        TOKEN_ROWS.append({
            "token_idx": STATE["token_idx"], "batch_idx": STATE["batch_idx"],
            "first_seen_at": str(token.first_seen_at), **rows,
            "source_rows_total": sum(rows.values()),
        })
        _emit("method", "source_row_load", t0, time.perf_counter(),
              time.process_time() - c0, rows=sum(rows.values()))


R._load_sources = _load_sources
for _meth, _lbl in (
    ("build_birth_event", "diff_creation"),
    ("build_snapshot", "diff_creation"),
    ("build_actor_observation", "diff_creation"),
    ("compute_survival", "survival_compute"),
    ("_universe", "selection"),
    ("universe_size", "selection"),
    ("backlog_size", "selection"),
    ("unreconciled_backlog", "selection"),
    ("oldest_unreconciled_first_seen_at", "selection"),
):
    _timed_method(R, _meth, _lbl)

_original_process_batch = R._process_batch


def _process_batch(self, *a, **kw):
    STATE["batch_idx"] += 1
    t0, c0 = time.perf_counter(), time.process_time()
    try:
        return _original_process_batch(self, *a, **kw)
    finally:
        _emit("batch", "process_batch", t0, time.perf_counter(),
              time.process_time() - c0)


R._process_batch = _process_batch
_original_commit = Session.commit


def _commit(self, *a, **kw):
    t0, c0 = time.perf_counter(), time.process_time()
    try:
        return _original_commit(self, *a, **kw)
    finally:
        t1 = time.perf_counter()
        opened = _open_txn[0]
        if opened is not None:
            TXN_WINDOWS.append([opened, t1])
            _open_txn[0] = None
        _emit("commit", "commit", t0, t1, time.process_time() - c0,
              txn_open_at=opened)


Session.commit = _commit


def _sleeper(seconds: float) -> None:
    t0 = time.perf_counter()
    time.sleep(seconds)
    _emit("sleep", "inter_batch_yield", t0, time.perf_counter(), 0.0)


# --- the pass ----------------------------------------------------------------
settings = get_settings()
recorder = R(ct.CryptoTapeConfig.from_settings(settings))
session = get_sessionmaker()()
_pass_t0 = time.perf_counter()
summary = recorder.run_once(
    session,
    limit=ARGS.limit,
    hours=ARGS.window_hours,
    dry_run=False,
    # exactly what run_scheduled_reconciliation passes:
    oldest_first=True,
    include_backlog=True,
    exclude_final=True,
    min_age_minutes=ct.SHORTEST_HORIZON_CLOSING_EDGE_MINUTES,
    run_config_extra={"mode": "scheduled_reconciliation", "forced": True,
                      "bench": "crypto_reconcile_cost_profile"},
    skip_redundant_when_final=True,
    batch_size=ARGS.batch_size,
    max_duration_seconds=ARGS.max_duration_seconds,
    sleeper=_sleeper,
)
PASS_WALL = time.perf_counter() - _pass_t0
session.close()


# --- aggregation -------------------------------------------------------------
def _overlap(a0: float, a1: float) -> float:
    total = 0.0
    for w0, w1 in TXN_WINDOWS:
        lo, hi = max(a0, w0), min(a1, w1)
        if hi > lo:
            total += hi - lo
    return total


def _pctl(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))]


components: dict[str, dict] = defaultdict(lambda: {
    "statements": 0, "cpu_s": 0.0, "db_read_s": 0.0, "db_write_s": 0.0,
    "rows_written": 0, "in_txn_s": 0.0, "out_txn_s": 0.0, "per_unit": [],
})
by_statement: dict[str, dict] = defaultdict(
    lambda: {"n": 0, "wall": 0.0, "in_txn": 0.0, "each": []})

for e in EVENTS:
    if e["kind"] == "sql":
        c = components[e["label"]]
        c["statements"] += 1
        c["cpu_s"] += e["cpu"]
        if e["io"] == "read":
            c["db_read_s"] += e["wall"]
        elif e["io"] == "write":
            c["db_write_s"] += e["wall"]
            if e["rowcount"] > 0:
                c["rows_written"] += e["rowcount"]
        inside = _overlap(e["t0"], e["t1"])
        c["in_txn_s"] += inside
        c["out_txn_s"] += e["wall"] - inside
        key = f"{e['verb'].split()[0]:6s} {e['table']}"
        s = by_statement[key]
        s["n"] += 1
        s["wall"] += e["wall"]
        s["in_txn"] += inside
        s["each"].append(e["wall"])
    elif e["kind"] in ("method", "commit", "sleep"):
        if e["label"] == "source_row_load":
            continue  # its SQL is already counted; avoid double counting
        c = components[e["label"]]
        c["cpu_s"] += e["cpu"]
        inside = _overlap(e["t0"], e["t1"])
        c["in_txn_s"] += inside
        c["out_txn_s"] += e["wall"] - inside
        c["per_unit"].append(e["wall"])

holds = [w1 - w0 for w0, w1 in TXN_WINDOWS]
hold_total = sum(holds) or 1.0
report = {
    "label": ARGS.label,
    "db": str(DB_PATH),
    "params": {
        "limit": ARGS.limit, "batch_size": ARGS.batch_size,
        "max_duration_seconds": ARGS.max_duration_seconds,
        "window_hours": ARGS.window_hours,
        "min_age_minutes": ct.SHORTEST_HORIZON_CLOSING_EDGE_MINUTES,
        "post_batch_yield_seconds": ct.RECONCILE_POST_BATCH_YIELD_SECONDS,
        "write_time_slo_seconds": ct.RECONCILE_WRITE_TIME_SLO_SECONDS,
    },
    "summary": {k: v for k, v in summary.items() if k not in (
        "examples", "provider_coverage", "survival_label_mix", "note")},
    "pass_wall_seconds": PASS_WALL,
    "write_txn_hold_seconds": {
        "n": len(holds), "p50": _pctl(holds, 0.5), "p95": _pctl(holds, 0.95),
        "max": max(holds) if holds else None, "sum": sum(holds),
        "p95_share_of_slo": (
            _pctl(holds, 0.95) / ct.RECONCILE_WRITE_TIME_SLO_SECONDS
            if holds else None),
    },
    "components": {
        name: {
            **{k: v for k, v in c.items() if k != "per_unit"},
            "wall_s": c["in_txn_s"] + c["out_txn_s"],
            "share_of_pass_wall": (c["in_txn_s"] + c["out_txn_s"]) / PASS_WALL,
            "share_of_write_lock_hold": c["in_txn_s"] / hold_total,
            "per_call_p50": _pctl(c["per_unit"], 0.5),
            "per_call_p95": _pctl(c["per_unit"], 0.95),
            "per_call_max": max(c["per_unit"]) if c["per_unit"] else None,
        }
        for name, c in sorted(
            components.items(),
            key=lambda kv: -(kv[1]["in_txn_s"] + kv[1]["out_txn_s"]))
    },
    "by_statement": {
        k: {"n": v["n"], "wall_s": v["wall"], "in_txn_s": v["in_txn"],
            "p50_ms": 1000 * _pctl(v["each"], 0.5),
            "p95_ms": 1000 * _pctl(v["each"], 0.95),
            "max_ms": 1000 * max(v["each"])}
        for k, v in sorted(by_statement.items(), key=lambda kv: -kv[1]["wall"])
    },
    "source_rows_per_token": {
        "n_tokens": len(TOKEN_ROWS),
        "p50": _pctl([t["source_rows_total"] for t in TOKEN_ROWS], 0.5),
        "p95": _pctl([t["source_rows_total"] for t in TOKEN_ROWS], 0.95),
        "max": max((t["source_rows_total"] for t in TOKEN_ROWS), default=None),
        "mean_by_table": {
            k: (sum(t[k] for t in TOKEN_ROWS) / len(TOKEN_ROWS))
            if TOKEN_ROWS else 0
            for k in ("pairs", "ticks", "assessments", "discovery_events",
                      "attention")
        },
    },
}

out_dir = Path(ARGS.out_dir)
out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / f"cost_profile_{ARGS.label}.json").write_text(
    json.dumps(report, indent=2, default=str))
(out_dir / f"cost_events_{ARGS.label}.json").write_text(
    json.dumps({"events": EVENTS, "txn_windows": TXN_WINDOWS,
                "token_rows": TOKEN_ROWS}, default=str))

print(json.dumps(report, indent=2, default=str))
print(f"\nwrote {out_dir / f'cost_profile_{ARGS.label}.json'}")
