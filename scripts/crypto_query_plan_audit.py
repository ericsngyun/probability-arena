#!/usr/bin/env python3
"""
crypto_query_plan_audit.py — DISPOSABLE query-plan + latency audit harness
(CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001, B1 and B2).

============================================================================
DO NOT RUN THIS AGAINST PRODUCTION. DO NOT POINT --db-path AT A REAL DB.
============================================================================

Why this exists
---------------
CRYPTO-RECONCILIATION-CAPACITY-001 established that the reconciler's capacity
blocker is a MISSING QUERY PLAN, not the write set, and that a single `ANALYZE`
raises throughput ~10x. But `ANALYZE` is a **global** planner change: it writes
`sqlite_stat1` once and every query in the application is then re-planned
against those statistics. A benchmark that only measures the reconciler cannot
authorise that change.

This harness therefore does two things the cost profiler does not:

  B1 — MECHANISM. For every expensive reconciliation source query it captures
       the REAL statement text and REAL bound parameters (via SQLAlchemy cursor
       hooks around the actual application code path — never a hand-retyped
       SQL string that could drift), then runs `EXPLAIN QUERY PLAN` for each,
       records the index chosen and whether a temp B-tree sort is used, and
       times the statement. Run it with and without `--analyze` on the same
       copy to get the before/after pair.

  B2 — REGRESSION AUDIT. The same capture/EQP/timing treatment applied to
       representative hot paths from the rest of the application (MarketOps,
       the watcher, the crypto scout, tick aggregation, forecast/outcome,
       retention/backup/db-growth, the scan pipeline), so a plan that gets
       WORSE after `ANALYZE` is visible rather than averaged away by the
       reconciler's own 10x win.

Everything here is read-only against the database EXCEPT `--analyze`, which
writes `sqlite_stat1` — that is why the path guard below refuses any deployment
looking path. Probes are chosen to be read-only application functions; no probe
mutates.

Usage (always against a throwaway copy, never a real deployment DB):

    .venv/bin/python scripts/crypto_query_plan_audit.py \\
        --db-path /scratch/copy.db --label before --out-dir /scratch
    .venv/bin/python scripts/crypto_query_plan_audit.py \\
        --db-path /scratch/copy.db --label after --out-dir /scratch --analyze

"Rows scanned" is reported where it is DERIVABLE FROM THE PLAN plus a real
COUNT (e.g. a `SEARCH ... USING INDEX ix_x_chain (chain=?)` over a table whose
every row has that chain scans the whole index). SQLite's scanstatus API is not
available from Python, so any number labelled `rows_scanned_derived` is an
inference from the plan and a measured count, not a direct instrument reading.
Latency is always directly measured.

It is intentionally NOT part of the pytest suite (real wall-clock seconds,
host-speed dependent) and is NOT imported by any application code.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DEPLOYMENT_MARKERS = ("projects/probability-arena/data", "/var/lib/", "/srv/")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--db-path", required=True,
                   help="throwaway SQLite copy to audit (NEVER a real DB)")
    p.add_argument("--label", default="run")
    p.add_argument("--out-dir", default=".")
    p.add_argument("--analyze", action="store_true",
                   help="run ANALYZE on the copy BEFORE probing (writes "
                        "sqlite_stat1 to the copy)")
    p.add_argument("--probes", default="recon,hot",
                   help="comma-separated probe groups: recon, hot")
    p.add_argument("--sample-tokens", type=int, default=40,
                   help="how many real tokens to drive _load_sources with")
    p.add_argument("--reps", type=int, default=3,
                   help="timing repetitions per probe")
    p.add_argument("--only", default="",
                   help="comma-separated probe names; empty = all in --probes")
    return p.parse_args()


ARGS = _parse_args()
DB_PATH = Path(ARGS.db_path).resolve()
for _frag in _DEPLOYMENT_MARKERS:
    if _frag in str(DB_PATH):
        raise SystemExit(
            f"refusing to audit a deployment-looking path: {DB_PATH}\n"
            "Point this at a throwaway copy."
        )
if not DB_PATH.exists():
    raise SystemExit(f"no such database: {DB_PATH}")

os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3  # noqa: E402

from sqlalchemy import event, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402

CAPTURE: list[dict] = []
_ACTIVE: dict = {"probe": None}
_stack: list[float] = []


engine = get_engine()


@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    _stack.append(time.perf_counter())


@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    t0 = _stack.pop()
    if _ACTIVE["probe"] is None:
        return
    CAPTURE.append({
        "probe": _ACTIVE["probe"],
        "sql": " ".join(statement.split()),
        "params": list(parameters) if isinstance(parameters, (list, tuple)) else parameters,
        "wall": time.perf_counter() - t0,
    })


def _pctl(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(p * (len(ordered) - 1)))))]


# --- EXPLAIN QUERY PLAN ------------------------------------------------------
RAW = sqlite3.connect(str(DB_PATH))


def explain(sql: str, params) -> dict:
    """EQP for one real statement with its real bound parameters."""
    try:
        rows = RAW.execute("EXPLAIN QUERY PLAN " + sql, params or []).fetchall()
    except sqlite3.Error as exc:  # pragma: no cover - diagnostic path
        return {"error": str(exc), "detail": [], "index": None,
                "temp_btree": None}
    detail = [r[3] for r in rows]
    joined = " | ".join(detail)
    index = None
    for d in detail:
        if "USING INDEX" in d:
            index = d.split("USING INDEX", 1)[1].split("(")[0].strip()
            break
        if "USING COVERING INDEX" in d:
            index = d.split("USING COVERING INDEX", 1)[1].split("(")[0].strip()
            break
    return {
        "detail": detail,
        "index": index,
        "scan": any(d.startswith("SCAN") for d in detail),
        "temp_btree": "TEMP B-TREE" in joined,
        "auto_index": "AUTOMATIC" in joined,
    }


# --- probe registry ----------------------------------------------------------
PROBES: list[tuple[str, str, object]] = []


def probe(name: str, group: str):
    def deco(fn):
        PROBES.append((name, group, fn))
        return fn
    return deco


settings = get_settings()
Sessionmaker = get_sessionmaker()

NOW = datetime.now(timezone.utc)


def _sample_tokens(session, n: int):
    from app.models import CryptoToken
    return list(session.execute(
        select(CryptoToken)
        .where(CryptoToken.chain == "solana")
        .order_by(CryptoToken.first_seen_at.asc(), CryptoToken.id.asc())
        .limit(n)
    ).scalars().all())


# ============================ B1 — reconciliation ============================
@probe("recon.load_sources", "recon")
def _p_load_sources(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    for tok in ctx["tokens"]:
        rec._load_sources(session, tok, NOW)


@probe("recon.universe", "recon")
def _p_universe(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    cutoff = NOW - timedelta(hours=48)
    rec._universe(
        session, 2000, cutoff, oldest_first=True, exclude_final=True,
        max_first_seen_at=NOW - timedelta(
            minutes=ct.SHORTEST_HORIZON_CLOSING_EDGE_MINUTES),
    )


@probe("recon.universe_size", "recon")
def _p_universe_size(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    cutoff = NOW - timedelta(hours=48)
    rec.universe_size(
        session, cutoff, exclude_final=True,
        max_first_seen_at=NOW - timedelta(
            minutes=ct.SHORTEST_HORIZON_CLOSING_EDGE_MINUTES),
    )


@probe("recon.unreconciled_backlog", "recon")
def _p_backlog(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    rec.unreconciled_backlog(session, NOW - timedelta(hours=48), limit=2000)


@probe("recon.backlog_size", "recon")
def _p_backlog_size(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    rec.backlog_size(session, NOW - timedelta(hours=48))


@probe("recon.oldest_unreconciled", "recon")
def _p_oldest(session, ctx):
    from app.services import crypto_tape as ct
    rec = ct.CryptoLifecycleTapeRecorder(ct.CryptoTapeConfig.from_settings(settings))
    rec.oldest_unreconciled_first_seen_at(session, NOW - timedelta(hours=48))


# ============================ B2 — hot paths =================================
# Every probe below calls a REAL production function that runs on a timer or in
# the always-on watcher loop on EVO. Only read-only functions (or documented
# dry-run paths) are used — no probe writes, and none makes a provider call.
# `run_once`-style entry points are deliberately NOT used: they write and call
# out to the network. Their constituent selectors are driven instead.


def _register_hot() -> None:
    def _try(name, fn):
        PROBES.append((name, "hot", fn))

    # --- MarketOps autopilot (5-minute timer on EVO) -------------------------
    def _ops():
        from app.services.marketops import MarketOpsAutopilotService
        return MarketOpsAutopilotService()   # constructs no adapter

    _try("marketops.active_run",
         lambda s, c: _ops()._active_run(s))
    _try("marketops.eligible_signals",
         lambda s, c: _ops()._eligible_signals(s, NOW))
    _try("marketops.recently_refreshed_tickers",
         lambda s, c: _ops()._recently_refreshed_tickers(s, NOW))
    _try("marketops.tickers_awaiting_processing",
         lambda s, c: _ops()._tickers_awaiting_processing(s))
    _try("marketops.select_signals_for_promotion",
         lambda s, c: _ops().select_signals_for_promotion(s, now=NOW))

    def marketops_growth(session, ctx):
        # the heaviest read in the application; runs every MarketOps cycle
        from app.services.db_growth import build_growth_report
        build_growth_report(session)
    _try("marketops.db_growth_report", marketops_growth)

    # --- watcher (always-on, ~60s loop) --------------------------------------
    def _watcher():
        from app.services.watcher import RealtimeWatcher
        return RealtimeWatcher(adapter=object())   # adapter never touched here

    _try("watcher.universe_tickers",
         lambda s, c: _watcher()._universe_tickers(s, 200))

    def watcher_latest_tick(session, ctx):
        from app.services.watcher import latest_tick_for
        for t in ctx["market_tickers"]:
            latest_tick_for(session, t)
    _try("watcher.latest_tick_for_x50", watcher_latest_tick)

    def watcher_last_signal(session, ctx):
        from app.services.watcher import _last_signal_at
        for t in ctx["market_tickers"]:
            _last_signal_at(session, t, "price_move")
    _try("watcher.last_signal_at_x50", watcher_last_signal)

    # --- crypto scout / meme scout -------------------------------------------
    def scout_token_probe(session, ctx):
        from app.models import CryptoToken
        for addr in ctx["token_addresses"][:50]:
            session.execute(
                select(CryptoToken).where(CryptoToken.chain == "solana",
                                          CryptoToken.token_address == addr)
            ).scalars().first()
    _try("crypto_scout.upsert_token_probe_x50", scout_token_probe)

    def scout_pair_tick(session, ctx):
        from app.services.crypto_scout import latest_tick_for_pair
        for p in ctx["pair_addresses"]:
            latest_tick_for_pair(session, p)
    _try("crypto_scout.latest_tick_for_pair_x50", scout_pair_tick)

    def scout_last_signal(session, ctx):
        from app.services.crypto_scout import CryptoSignalService
        svc = CryptoSignalService()
        for addr in ctx["token_addresses"][:50]:
            svc._last_signal_at(session, addr, "risk_flag")
    _try("crypto_scout.last_signal_at_x50", scout_last_signal)

    def scout_report(session, ctx):
        from app.services.crypto_scout import CryptoReportService
        CryptoReportService().build(session, recent_limit=10)
    _try("crypto_scout.report_build", scout_report)

    def meme_risk_overlay(session, ctx):
        from app.services.meme_scout import MemeScoutService
        svc = MemeScoutService.__new__(MemeScoutService)
        for addr in ctx["token_addresses"][:20]:
            svc._risk_overlay(session, addr)
    _try("meme_scout.risk_overlay_x20", meme_risk_overlay)

    # --- tick aggregation (hourly timer) -------------------------------------
    def tick_agg_dry(session, ctx):
        from app.services.tick_aggregation import TickAggregationService
        TickAggregationService().aggregate(session, hours=12, dry_run=True)
    _try("tick_agg.aggregate_dry_run_12h", tick_agg_dry)

    def tick_agg_report(session, ctx):
        from app.services.tick_aggregation import TickAggregationReportService
        TickAggregationReportService().build(session)
    _try("tick_agg.report_build", tick_agg_report)

    # --- forecast / outcome --------------------------------------------------
    def outcome_candidates(session, ctx):
        from app.services.outcomes import OutcomeService
        # __new__ avoids constructing the Kalshi adapter; this selector needs
        # no adapter (outcome_coverage.audit_selection does the same thing).
        OutcomeService.__new__(OutcomeService).select_sync_candidates(session, 100)
    _try("outcome.select_sync_candidates", outcome_candidates)

    def calib_candidates(session, ctx):
        from app.services.calibration import CalibrationService
        CalibrationService().select_scoring_candidates(session, 500)
    _try("calibration.select_scoring_candidates", calib_candidates)

    def calib_summary(session, ctx):
        from app.services.calibration import CalibrationService
        CalibrationService().summary(session)
    _try("calibration.summary", calib_summary)

    def cc_compare(session, ctx):
        from app.services.champion_challenger import ChampionChallengerService
        ChampionChallengerService().compare(session, min_count=1)
    _try("champion_challenger.compare", cc_compare)

    def coverage_report(session, ctx):
        from app.services.outcome_coverage import build_coverage_report
        build_coverage_report(session)
    _try("outcome_coverage.build_report", coverage_report)

    # --- retention (daily timer) ---------------------------------------------
    def retention_report(session, ctx):
        from app.services.retention import RetentionService
        RetentionService().prune_report(session)
    _try("retention.prune_report", retention_report)

    # --- scan pipeline (4-hourly baseline timer) ------------------------------
    def pipeline_candidates(session, ctx):
        from app.services.pipeline import _top_candidates
        _top_candidates(session, 200)
    _try("pipeline.top_candidates", pipeline_candidates)

    # --- signal workflow ------------------------------------------------------
    def signal_report(session, ctx):
        from app.services.signal_workflow import build_signal_report
        build_signal_report(session)
    _try("signal_workflow.build_report", signal_report)

    def promote_top_selection(session, ctx):
        from app.models import OpportunitySignal
        from app.services.signal_workflow import STATUS_NEW
        session.execute(
            select(OpportunitySignal).where(
                OpportunitySignal.signal_status == STATUS_NEW)
        ).scalars().all()
    _try("signal_workflow.promote_top_selection", promote_top_selection)


_register_hot()


# --- driver ------------------------------------------------------------------
def main() -> int:
    analyze_seconds = None
    if ARGS.analyze:
        t0 = time.perf_counter()
        RAW.execute("ANALYZE")
        RAW.commit()
        analyze_seconds = time.perf_counter() - t0
        # statistics are loaded with the schema; the ORM engine must see them.
        engine.dispose()

    stat1 = []
    try:
        stat1 = RAW.execute(
            "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx"
        ).fetchall()
    except sqlite3.Error:
        stat1 = []

    groups = {g.strip() for g in ARGS.probes.split(",") if g.strip()}
    session = Sessionmaker()
    ctx = {"tokens": _sample_tokens(session, ARGS.sample_tokens)}
    ctx["token_addresses"] = [t.token_address for t in ctx["tokens"]]
    from app.models import CryptoPair, Market  # noqa: E402
    ctx["pair_addresses"] = list(session.execute(
        select(CryptoPair.pair_address).limit(50)).scalars().all())
    ctx["market_tickers"] = list(session.execute(
        select(Market.ticker).order_by(Market.id.desc()).limit(50)
    ).scalars().all())

    results: dict[str, dict] = {}
    only = {n.strip() for n in ARGS.only.split(",") if n.strip()}
    for name, group, fn in PROBES:
        if group not in groups or (only and name not in only):
            continue
        walls = []
        errors = []
        for rep in range(ARGS.reps):
            CAPTURE.clear()
            _ACTIVE["probe"] = name
            t0 = time.perf_counter()
            try:
                fn(session, ctx)
            except Exception as exc:  # a missing model/column is data, not fatal
                errors.append(f"{type(exc).__name__}: {exc}")
                session.rollback()
                _ACTIVE["probe"] = None
                break
            walls.append(time.perf_counter() - t0)
            _ACTIVE["probe"] = None
            captured = list(CAPTURE)
        if errors:
            results[name] = {"group": group, "error": errors[0]}
            continue

        per_stmt: dict[str, dict] = defaultdict(
            lambda: {"n": 0, "wall": 0.0, "each": [], "params": None})
        for c in captured:
            s = per_stmt[c["sql"]]
            s["n"] += 1
            s["wall"] += c["wall"]
            s["each"].append(c["wall"])
            if s["params"] is None:
                s["params"] = c["params"]

        stmts = []
        for sql, s in sorted(per_stmt.items(), key=lambda kv: -kv[1]["wall"]):
            plan = explain(sql, s["params"])
            stmts.append({
                "sql": sql[:400],
                "n": s["n"],
                "wall_s": round(s["wall"], 6),
                "p50_ms": round(1000 * _pctl(s["each"], 0.5), 4),
                "p95_ms": round(1000 * _pctl(s["each"], 0.95), 4),
                "max_ms": round(1000 * max(s["each"]), 4),
                "plan": plan["detail"],
                "index": plan.get("index"),
                "full_scan": plan.get("scan"),
                "temp_btree": plan.get("temp_btree"),
            })
        results[name] = {
            "group": group,
            "probe_wall_s": {
                "reps": len(walls),
                "p50": round(statistics.median(walls), 6),
                "min": round(min(walls), 6),
                "max": round(max(walls), 6),
                "all": [round(w, 6) for w in walls],
            },
            "statements": stmts,
        }

    session.close()
    report = {
        "label": ARGS.label,
        "db": str(DB_PATH),
        "db_bytes": DB_PATH.stat().st_size,
        "analyzed": ARGS.analyze,
        "analyze_seconds": analyze_seconds,
        "sqlite_version": sqlite3.sqlite_version,
        "sample_tokens": len(ctx["tokens"]),
        "reps": ARGS.reps,
        "note": (
            "probe_wall_s is the authoritative latency. Per-statement wall "
            "times come from SQLAlchemy cursor hooks, which close when "
            "cursor.execute() returns — for a query that needs a sorter or an "
            "aggregate that is the whole cost, but for a streaming query part "
            "of the cost lands in fetch and is therefore UNDER-reported at the "
            "statement level. Compare probes by probe_wall_s; use the "
            "statement rows for the plan, not for a cost total."
        ),
        "sqlite_stat1_rows": len(stat1),
        "sqlite_stat1": [{"tbl": t, "idx": i, "stat": s} for t, i, s in stat1],
        "probes": results,
    }
    out = Path(ARGS.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"query_plan_{ARGS.label}.json"
    path.write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items()
                      if k not in ("sqlite_stat1", "probes")}, indent=2))
    for name, r in results.items():
        if "error" in r:
            print(f"  {name:34s} ERROR {r['error'][:90]}")
            continue
        w = r["probe_wall_s"]
        idxs = {s["index"] for s in r["statements"] if s["index"]}
        tb = sum(1 for s in r["statements"] if s["temp_btree"])
        print(f"  {name:34s} p50={w['p50']*1000:9.2f}ms  stmts={len(r['statements'])}"
              f"  temp_btree={tb}  idx={sorted(i for i in idxs if i)}")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
