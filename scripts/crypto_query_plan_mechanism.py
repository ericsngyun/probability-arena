#!/usr/bin/env python3
"""
crypto_query_plan_mechanism.py — DISPOSABLE harness that isolates WHICH
`sqlite_stat1` row is responsible for each planner decision
(CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001, B1).

============================================================================
DO NOT RUN THIS AGAINST PRODUCTION. DO NOT POINT --db-path AT A REAL DB.
============================================================================

`crypto_query_plan_audit.py` shows the plan BEFORE and AFTER `ANALYZE`. That
proves the benchmark, not the mechanism: `ANALYZE` writes ~130 statistics rows
at once, so "ANALYZE fixed it" is compatible with any of them being the cause.

This script writes `sqlite_stat1` rows BY HAND, one subset at a time, and
re-opens the connection between subsets (SQLite loads statistics with the
schema, so a fresh connection is what makes a hand-written stat row take
effect). Each variant reports the resulting `EXPLAIN QUERY PLAN` and the
measured latency of the real query. The variant at which the plan flips
identifies the responsible statistic exactly.

`sqlite_stat1` format, for reference:
    (tbl, idx, stat)  where stat = "<rows-in-index> <avg rows per equal
    prefix of 1 column> <... per 2 columns> ..."
    A row with idx = NULL carries the table's own row estimate.
With no row at all, SQLite falls back to a fixed guess for an indexed equality
constraint, which is what makes a one-distinct-value index look as good as a
unique one.

Usage (always against a throwaway copy, never a real deployment DB):

    .venv/bin/python scripts/crypto_query_plan_mechanism.py \\
        --db-path /scratch/mech.db --out /scratch/mechanism.json

The script WRITES `sqlite_stat1` to --db-path (and nothing else). It is not
part of the pytest suite and is imported by no application code.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
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

# The real statements, verbatim in shape from app/services/crypto_tape.py
# `_load_sources` / `_universe` / `unreconciled_backlog` (the ORM emits the
# same predicates and ORDER BY; `SELECT *` stands in for the column list,
# which does not affect index selection).
QUERIES = {
    "discovery_events": (
        "SELECT * FROM crypto_token_discovery_events "
        "WHERE chain=? AND token_address=? ORDER BY observed_at, id",
        "crypto_token_discovery_events",
    ),
    "risk_assessments": (
        "SELECT * FROM crypto_token_risk_assessments "
        "WHERE chain=? AND token_address=? ORDER BY created_at, id",
        "crypto_token_risk_assessments",
    ),
    "price_ticks": (
        "SELECT * FROM crypto_price_ticks "
        "WHERE chain=? AND token_address=? ORDER BY observed_at, id",
        "crypto_price_ticks",
    ),
    "pairs": (
        "SELECT * FROM crypto_pairs "
        "WHERE chain=? AND base_token_address=? ORDER BY id",
        "crypto_pairs",
    ),
}

# The selection query, whose plan moves the OTHER way (see B1 findings).
UNIVERSE_SQL = (
    "SELECT DISTINCT t.* FROM crypto_tokens t "
    "LEFT OUTER JOIN crypto_token_survival_outcomes o "
    "  ON o.token_address = t.token_address AND o.chain = t.chain "
    "WHERE t.chain=? AND t.first_seen_at < ? "
    "  AND (o.id IS NULL OR o.final = 0) "
    "ORDER BY t.first_seen_at ASC, t.id ASC LIMIT 2000"
)


def eqp(conn: sqlite3.Connection, sql: str, params) -> list[str]:
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def conn_rowcount(db: Path, table: str) -> int:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


def chosen_index(detail: list[str], table: str, alias: str | None = None) -> str | None:
    """The index driving `table`. `alias` matters: EQP prints the ALIAS, not
    the table name, for an aliased FROM — without it a `SCAN t` reads back as
    "no index found" and a plan change is silently recorded as None."""
    names = [n for n in (table, alias) if n]
    for d in detail:
        if any(n in d for n in names) and "USING INDEX" in d:
            return d.split("USING INDEX", 1)[1].split("(")[0].strip()
        if any(n in d for n in names) and "USING COVERING INDEX" in d:
            return d.split("USING COVERING INDEX", 1)[1].split("(")[0].strip()
        if any(d.startswith(f"SCAN {n}") for n in names):
            return "<table scan>"
    return None


def timed(conn: sqlite3.Connection, sql: str, param_sets) -> float:
    """median wall ms of the fully-fetched query across param sets."""
    times = []
    for p in param_sets:
        t0 = time.perf_counter()
        conn.execute(sql, p).fetchall()
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return round(times[len(times) // 2], 4)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-path", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--samples", type=int, default=5)
    args = ap.parse_args()

    db = Path(args.db_path).resolve()
    _refuse_non_scratch(db)
    if not db.exists():
        raise SystemExit(f"no such database: {db}")

    # Full statistics, captured once, so each variant can replay real rows.
    boot = sqlite3.connect(str(db))
    boot.execute("ANALYZE")
    boot.commit()
    full_stat = boot.execute("SELECT tbl, idx, stat FROM sqlite_stat1").fetchall()
    addrs = [r[0] for r in boot.execute(
        "SELECT token_address FROM crypto_tokens ORDER BY first_seen_at "
        f"LIMIT {args.samples}")]
    # The SAME cutoff `unreconciled_backlog` uses (now − window_hours), not
    # max(first_seen_at). Binding the maximum would select every token in the
    # table and produce a timing that cannot be compared with the audit
    # harness's measurement of the real, 48h-bounded selection.
    cutoff = boot.execute(
        "SELECT datetime('now', '-48 hours')").fetchone()[0]
    boot.close()

    by_tbl: dict[str, list[tuple]] = {}
    for tbl, idx, stat in full_stat:
        by_tbl.setdefault(tbl, []).append((tbl, idx, stat))

    def set_stat(rows: list[tuple]) -> sqlite3.Connection:
        """Replace sqlite_stat1 with exactly `rows`, then hand back a FRESH
        connection — statistics are loaded with the schema, so the same
        connection would keep using the stats it already read."""
        c = sqlite3.connect(str(db))
        c.execute("DELETE FROM sqlite_stat1")
        c.executemany("INSERT INTO sqlite_stat1 (tbl, idx, stat) VALUES (?,?,?)", rows)
        c.commit()
        c.close()
        return sqlite3.connect(str(db))

    results: dict[str, list[dict]] = {}
    params = [("solana", a) for a in addrs]

    for qname, (sql, table) in QUERIES.items():
        rows_for_table = by_tbl.get(table, [])
        chain_idx = f"ix_{table}_chain"
        sel_col = "base_token_address" if table == "crypto_pairs" else "token_address"
        sel_idx = f"ix_{table}_{sel_col}"

        # SQLite only writes an `idx IS NULL` row for a table that has NO
        # indexes, so filtering ANALYZE's own output for it selects nothing on
        # every table here — V1 would silently be a duplicate of V0 and any
        # conclusion drawn from it would be a conclusion drawn from an empty
        # test. Synthesize the row instead, so V1 actually tests what it claims
        # to: does the table's row-count estimate alone move the planner?
        table_rows = conn_rowcount(db, table)
        variants = {
            "V0_no_statistics": [],
            "V1_synthetic_table_row_only": [(table, None, str(table_rows))],
            "V2_chain_index_only": [r for r in rows_for_table if r[1] == chain_idx],
            "V3_selective_index_only": [r for r in rows_for_table if r[1] == sel_idx],
            "V4_both_index_rows": [
                r for r in rows_for_table if r[1] in (chain_idx, sel_idx)],
            "V5_full_ANALYZE_for_this_table": rows_for_table,
            "V6_full_ANALYZE_everything": full_stat,
        }
        out = []
        for vname, rows in variants.items():
            conn = set_stat(rows)
            detail = eqp(conn, sql, params[0])
            ms = timed(conn, sql, params)
            conn.close()
            out.append({
                "variant": vname,
                "stat_rows": [{"tbl": t, "idx": i, "stat": s} for t, i, s in rows],
                "plan": detail,
                "index": chosen_index(detail, table),
                "temp_btree": any("TEMP B-TREE" in d for d in detail),
                "median_ms": ms,
            })
        results[qname] = out

    # the selection query, whose plan moves the other way
    tok_rows = by_tbl.get("crypto_tokens", [])
    uni = []
    for vname, rows in {
        "V0_no_statistics": [],
        "V2_chain_index_only": [r for r in tok_rows if r[1] == "ix_crypto_tokens_chain"],
        "V2b_chain_address_index_only": [
            r for r in tok_rows if r[1] == "ix_crypto_tokens_chain_address"],
        "V4_both_index_rows": [
            r for r in tok_rows
            if r[1] in ("ix_crypto_tokens_chain", "ix_crypto_tokens_chain_address")],
        "V6_full_ANALYZE_everything": full_stat,
    }.items():
        conn = set_stat(rows)
        detail = eqp(conn, UNIVERSE_SQL, ("solana", cutoff))
        # median of `--samples`, not a single execution: the four-table path
        # medians and this one must be comparable, and a lone timing on a
        # multi-GB file is jitter, not a measurement.
        ms = timed(conn, UNIVERSE_SQL, [("solana", cutoff)] * args.samples)
        conn.close()
        uni.append({
            "variant": vname,
            "stat_rows": [{"tbl": t, "idx": i, "stat": s} for t, i, s in rows],
            "plan": detail,
            "index": chosen_index(detail, "crypto_tokens", alias="t"),
            "temp_btree": any("TEMP B-TREE" in d for d in detail),
            "median_ms": ms,
        })
    results["unreconciled_backlog_selection"] = uni

    Path(args.out).write_text(json.dumps(results, indent=2))
    for qname, variants in results.items():
        print(f"\n=== {qname}")
        for v in variants:
            print(f"  {v['variant']:34s} idx={str(v['index']):48s} "
                  f"tempB={str(v['temp_btree']):5s} {v['median_ms']:9.3f} ms")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
