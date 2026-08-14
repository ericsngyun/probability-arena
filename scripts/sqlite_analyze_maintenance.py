#!/usr/bin/env python3
"""
sqlite_analyze_maintenance.py — deliberate, operator-controlled `ANALYZE`
maintenance action with a full before/after evidence record
(CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001, B8).

============================================================================
This is the ONE script in this repo that is meant to touch a live database,
and it is deliberately hard to run by accident. It is NOT imported by any
application code, NOT wired into any systemd unit, NOT called by MarketOps,
the watcher, the retention timer, or the backup timer, and it must never be.
`ANALYZE` changes the query planner's behaviour for EVERY query in the
application; that is a maintenance decision a human makes with evidence in
front of them, not something a timer does at 03:07 while nobody is looking.
============================================================================

What it does, in order:

  1. PREFLIGHT (all must pass, or it refuses and changes nothing):
       * the target file exists and is a SQLite database
       * a backup exists and is younger than --max-backup-age-hours
       * `PRAGMA quick_check` is `ok` on the target
       * the Alembic revision matches --expect-alembic (no migration is
         involved in ANALYZE; this is a tripwire against running it against
         the wrong file or a half-migrated one)
       * the operator passed --yes-run-analyze-on <the exact same path> and
         --confirm "<the exact confirmation phrase>"
  2. RECORD BEFORE: file bytes, page_size/page_count/freelist_count,
     journal_mode, busy_timeout, sqlite_stat1 presence and row count, the
     lock-telemetry event tally.
  3. RUN `ANALYZE`, timed.
  4. RECORD AFTER: everything from step 2 again, plus `PRAGMA integrity_check`
     and the new `sqlite_stat1` contents.

Nothing else is written. No schema change, no migration, no data change:
`ANALYZE` creates/updates exactly `sqlite_stat1`.

Usage:
    .venv/bin/python scripts/sqlite_analyze_maintenance.py \\
        --db-path /path/to/probability_arena.db \\
        --backup-dir /mnt/data/probability-arena-backups \\
        --expect-alembic 0027 \\
        --yes-run-analyze-on /path/to/probability_arena.db \\
        --confirm "I have read the B2 regression audit and the B3 gate" \\
        --out /scratch/analyze_evidence.json

Add --dry-run to execute every preflight check and record the BEFORE state
without running `ANALYZE`.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIRM_PHRASE = "I have read the B2 regression audit and the B3 gate"

# GATE2-WRITER-TELEMETRY-001 (security re-review, C2) — THE POPULATION
# `lock_events` IS COUNTED OVER, and the reason it has to be named.
#
# `_lock_tally` was written when exactly two writers appended to the 001A JSONL
# sink, and for both of them one record is one OPERATION: a non-zero
# `lock_wait_ms` on a `tick_aggregation` or `backup` record means that
# operation waited on somebody else's write lock. The `> 6` stop condition in
# docs/KALSHI_DEMO_READONLY_VALIDATION_2026_08.md was read off that population.
#
# GATE2 added a second GRAIN to the same file: one record per writer PASS.
# A pass makes many lock acquisitions and its `lock_wait_ms` is their SUM,
# which carries a known systematic floor — one `run_row` sample and one
# `finalize` sample per pass that are once-per-pass bookkeeping, not
# contention. Measured on eight healthy, fully UNCONTENDED reconciler passes
# IN THE GATE2-WRITER-TELEMETRY-001 SESSION (the series lands here in 4147bcd):
# `lock_wait_ms` 21/11/11/10/9/9/14/10 ms, every one of which the flat
# predicate below counts, against `batch_lock_wait_ms_max` of 3/3/2/1/1/1/6/2
# ms — i.e. nothing contended. Eight passes therefore crossed `> 6` on their
# own, and the calibration session for `initial_per_token_cost_seconds` is
# itself about eight attended `--force` passes.
#
# NAME THE SESSION WHEN YOU QUOTE THESE. Three eight-value
# `batch_lock_wait_ms_max` series are in circulation and they are DIFFERENT
# populations, not restatements of one:
#   * 3/3/2/1/1/1/6/2  — GATE2-WRITER-TELEMETRY-001, uncontended passes. THIS
#     is the series the scoping argument below stands on.
#   * the CALIBRATION-GATE3-001 per-pass table (EVO-X2, ef92b4d) — 516/938/
#     514/4038/1015/..., i.e. passes that DID wait; see the runbook's Gate 3
#     subsection. Quoting it here would not support the argument below.
#   * 6/5/1/1/2/1/2/2   — quoted in review, not recorded in this repository.
#     Treat it as unsourced until a session is attached to it.
# The scoping conclusion survives all three; the provenance does not, so keep
# the label attached to the numbers.
#
# THE THRESHOLD IS NOT RAISED. Raising a limit to accommodate a miscount
# destroys the safeguard; the defect was never the limit, it was the
# population being counted under it. So the count is scoped back to the
# operation-grain writers the constant was derived from.
#
# WHY NOT `batch_lock_wait_ms_max` (the other offered fix): on the numbers
# above it does not work as stated — the uncontended passes measure 1-6 ms on
# that field too, so `> 0` counts them exactly as the flat predicate does.
# Making it work needs a millisecond threshold, and the only one available is
# the reconciler's own breaker constant — importing that here would let
# guarded code widen the boundary that governs it. Scoping needs no constant.
#
# Out-of-scope writers are NOT hidden: they are reported separately below, so a
# genuinely contended reconciler pass is still visible in the ANALYZE record.
# It simply cannot move a number calibrated on a different population.
LOCK_EVENT_WRITERS = frozenset({"tick_aggregation", "backup"})


def _state(path: Path, telemetry: Path | None) -> dict:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    st = {
        "file_bytes": path.stat().st_size,
        "page_size": conn.execute("PRAGMA page_size").fetchone()[0],
        "page_count": conn.execute("PRAGMA page_count").fetchone()[0],
        "freelist_count": conn.execute("PRAGMA freelist_count").fetchone()[0],
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        # NOTE: busy_timeout is a property of THIS connection, not of the
        # database file. Recording it unqualified would read as "production
        # runs at 5000 ms", which is false — the application sets 30000 via
        # `app.db.connect_args_for`. Named so it cannot be misread.
        "busy_timeout_of_this_readonly_connection": conn.execute(
            "PRAGMA busy_timeout").fetchone()[0],
        "user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    }
    st["freelist_share"] = (round(st["freelist_count"] / st["page_count"], 4)
                            if st["page_count"] else None)
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'sqlite_stat%'")]
    st["sqlite_stat_tables"] = names
    st["sqlite_stat1_rows"] = (
        conn.execute("SELECT count(*) FROM sqlite_stat1").fetchone()[0]
        if "sqlite_stat1" in names else 0)
    try:
        st["alembic_version"] = conn.execute(
            "SELECT version_num FROM alembic_version").fetchone()[0]
    except sqlite3.Error:
        st["alembic_version"] = None
    conn.close()
    st["lock_telemetry"] = _lock_tally(telemetry) if telemetry else None
    return st


def _lock_tally(path: Path) -> dict:
    """SQLITE-LOCK-TELEMETRY-001A's append-only JSONL sink. Counted, never
    modified. A malformed tail line is counted, not fatal.

    `lock_events` IS SCOPED TO `LOCK_EVENT_WRITERS` — see the constant above
    for why. Records from any other writer are counted in
    `out_of_scope_events`, and the subset of them that WOULD have tripped the
    flat predicate is reported as `out_of_scope_flat_predicate_hits` so the
    scoping is visible and auditable rather than silent.

    NOTE, not fixed here (GATE2 C2): this does `path.read_text()` on the whole
    file — the same class of whole-file slurp the runbook warns about for
    `app.telemetry.sink.read_events`, but NOT the same cost. **This function's
    own measured cost is ~204 MB peak Python heap on a 60.8 MB file — 3.4x
    amplification** (Gate 4 review, which ran both readers against that one
    file; `read_events` measured 453 MB, 7.2x, because it holds every parsed
    event while this discards each line after counting it). The 406 MB / 58 MB
    figure previously cited here was `read_events`', not this function's.
    Cheaper is not cheap: 3.4x of a file that nothing rotates before 001E is
    still the reason to check its size before running this script on EVO. The
    same paired measurement is in the runbook's growth section."""
    scope = sorted(LOCK_EVENT_WRITERS)
    if not path.exists():
        return {"file": str(path), "exists": False,
                "lock_event_scope": scope}
    total = 0
    lock_events = 0
    malformed = 0
    out_of_scope_events = 0
    out_of_scope_flat_predicate_hits = 0
    last_lock = None
    last_ts = None
    for line in path.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except Exception:
            malformed += 1
            continue
        total += 1
        last_ts = e.get("started_at", last_ts)
        waited = ((e.get("lock_wait_ms") or 0) > 0
                  or (e.get("retry_count") or 0) > 0)
        if e.get("writer_name") not in LOCK_EVENT_WRITERS:
            out_of_scope_events += 1
            out_of_scope_flat_predicate_hits += int(waited)
            continue
        if waited:
            lock_events += 1
            last_lock = {
                "started_at": e.get("started_at"),
                "operation_name": e.get("operation_name"),
                "writer_name": e.get("writer_name"),
                "lock_wait_ms": e.get("lock_wait_ms"),
                "retry_count": e.get("retry_count"),
                "outcome": e.get("outcome"),
            }
    return {"file": str(path), "exists": True, "events": total,
            "lock_events": lock_events, "malformed_lines": malformed,
            "last_event_at": last_ts, "last_lock_event": last_lock,
            "lock_event_scope": scope,
            "out_of_scope_events": out_of_scope_events,
            "out_of_scope_flat_predicate_hits": out_of_scope_flat_predicate_hits}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-path", required=True)
    ap.add_argument("--yes-run-analyze-on", required=True,
                    help="must equal --db-path exactly")
    ap.add_argument("--confirm", required=True)
    ap.add_argument("--backup-dir", required=True)
    ap.add_argument("--expect-alembic", required=True)
    ap.add_argument("--max-backup-age-hours", type=float, default=36.0)
    ap.add_argument("--min-backup-bytes", type=int, default=50_000_000,
                    help="a plausible compressed backup floor; a "
                         "truncated archive is not a rollback path")
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--telemetry-file",
                    default=str(Path.home() /
                                "probability-arena-telemetry/sqlite-writes.jsonl"))
    args = ap.parse_args()

    db = Path(args.db_path).resolve()
    now = datetime.now(timezone.utc)
    preflight: dict = {"checks": {}, "passed": True}

    def check(name: str, ok: bool, detail) -> None:
        preflight["checks"][name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            preflight["passed"] = False

    check("db_exists", db.exists(), str(db))
    check("confirm_path_matches",
          Path(args.yes_run_analyze_on).resolve() == db,
          f"{args.yes_run_analyze_on!r} vs {db}")
    check("confirm_phrase", args.confirm == CONFIRM_PHRASE,
          "exact phrase required")

    backups = sorted(Path(args.backup_dir).glob("backup-*.db.gz")) \
        if Path(args.backup_dir).is_dir() else []
    newest = backups[-1] if backups else None
    age_h = ((now.timestamp() - newest.stat().st_mtime) / 3600.0
             if newest else None)
    # Freshness alone is a weak gate: a zero-byte or truncated
    # `backup-*.db.gz` would pass the one check whose entire purpose is "we can
    # restore if this goes wrong". Verify the gzip stream actually decodes and
    # is not trivially small before treating it as a rollback path.
    backup_bytes = newest.stat().st_size if newest else 0
    gzip_ok = False
    gzip_error = None
    if newest is not None:
        try:
            with gzip.open(newest, "rb") as fh:
                head = fh.read(16)
                while fh.read(8 * 1024 * 1024):
                    pass
            gzip_ok = head.startswith(b"SQLite format 3")
            if not gzip_ok:
                gzip_error = "decompressed stream is not a SQLite database"
        except Exception as exc:
            gzip_error = f"{type(exc).__name__}: {exc}"
    check("backup_fresh",
          newest is not None and age_h is not None
          and age_h <= args.max_backup_age_hours,
          {"newest": newest.name if newest else None,
           "age_hours": round(age_h, 2) if age_h is not None else None,
           "limit_hours": args.max_backup_age_hours})
    check("backup_restorable",
          gzip_ok and backup_bytes >= args.min_backup_bytes,
          {"bytes": backup_bytes, "min_bytes": args.min_backup_bytes,
           "gzip_decodes_to_sqlite": gzip_ok, "error": gzip_error})

    quick = None
    alembic = None
    if db.exists():
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        quick = c.execute("PRAGMA quick_check").fetchone()[0]
        try:
            alembic = c.execute(
                "SELECT version_num FROM alembic_version").fetchone()[0]
        except sqlite3.Error:
            alembic = None
        c.close()
    check("quick_check_ok", quick == "ok", quick)
    check("alembic_matches", str(alembic) == str(args.expect_alembic),
          {"found": alembic, "expected": args.expect_alembic})

    telemetry = Path(args.telemetry_file)
    before = _state(db, telemetry) if db.exists() else None

    record = {
        "action": "ANALYZE",
        "db": str(db),
        "started_at": now.isoformat(),
        "dry_run": args.dry_run,
        "preflight": preflight,
        "before": before,
    }

    if not preflight["passed"]:
        record["result"] = "REFUSED — preflight failed; nothing was changed"
        Path(args.out).write_text(json.dumps(record, indent=2, default=str))
        print(json.dumps(record, indent=2, default=str))
        return 3
    if args.dry_run:
        record["result"] = "DRY RUN — preflight passed; ANALYZE not executed"
        Path(args.out).write_text(json.dumps(record, indent=2, default=str))
        print(json.dumps(record, indent=2, default=str))
        return 0

    conn = sqlite3.connect(str(db), timeout=30.0)
    conn.execute("PRAGMA busy_timeout = 30000")
    t0 = time.perf_counter()
    conn.execute("ANALYZE")
    conn.commit()
    duration = time.perf_counter() - t0
    conn.close()
    # give the OS a moment so the AFTER file size is the settled one
    time.sleep(0.5)

    after = _state(db, telemetry)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
    stat1 = c.execute(
        "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY tbl, idx").fetchall()
    c.close()

    record.update({
        "analyze_seconds": round(duration, 4),
        "after": after,
        "integrity_check": integrity,
        "sqlite_stat1": [{"tbl": t, "idx": i, "stat": s} for t, i, s in stat1],
        "delta": {
            "file_bytes": after["file_bytes"] - before["file_bytes"],
            "page_count": after["page_count"] - before["page_count"],
            "freelist_count": after["freelist_count"] - before["freelist_count"],
            "sqlite_stat1_rows": (after["sqlite_stat1_rows"]
                                  - before["sqlite_stat1_rows"]),
            "lock_events": (
                (after["lock_telemetry"] or {}).get("lock_events", 0)
                - (before["lock_telemetry"] or {}).get("lock_events", 0)),
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "result": "EXECUTED",
    })
    Path(args.out).write_text(json.dumps(record, indent=2, default=str))
    printable = {k: v for k, v in record.items() if k != "sqlite_stat1"}
    print(json.dumps(printable, indent=2, default=str))
    print(f"\nwrote {args.out} ({len(stat1)} sqlite_stat1 rows recorded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
