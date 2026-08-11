#!/usr/bin/env python3
"""
crypto_backlog_partition.py — arrivals and an EXACT, typed partition of the
crypto reconciliation backlog
(CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001, B4 and B5).

============================================================================
Read-only. Point --db-path at a COPY anyway; it opens the file `mode=ro`.
============================================================================

B4 — arrivals. Token arrivals over 24 h / 3 d / 7 d / 14 d / 30 d from
`crypto_tokens.first_seen_at`, plus the per-UTC-calendar-day distribution and
a p95 daily estimate over complete days only.

B5 — backlog partition. Every backlog token (outside the 48 h window, outcome
row missing or `final = 0`) is assigned EXACTLY ONE class. The classes are not
invented here: they are read straight off `CryptoLifecycleTapeRecorder.
compute_survival` (`app/services/crypto_tape.py:1138-1300`), which is the
function that decides what a pass can actually learn about a token:

  RETENTION_LOST                  window closed, no `survived_24h` was ever
                                  recorded, and the LATEST tick that could
                                  still have qualified (one landing exactly on
                                  the closing edge, anchor + 24 h*(1+tol)) is
                                  now older than `crypto_retention_days`, so
                                  `retention.py` has already deleted it. A pass
                                  can only write this token off.
  RECOVERABLE_NOW                 window closed, no answer recorded, and a real
                                  persisted tick sits inside the 24 h horizon
                                  tolerance AND an initial liquidity is known
                                  — a pass run right now produces a genuine
                                  `survived_24h` and finalises the token.
  PARTIALLY_RECOVERABLE           no qualifying 24 h tick, but a tick sits
                                  inside the 15 m / 1 h / 6 h tolerance, so a
                                  pass learns real sub-horizon labels while the
                                  24 h answer stays permanently unavailable.
  MISSING_REQUIRED_INITIAL_STATE  no first-evidence anchor at all, or no
                                  initial liquidity to compare against — the
                                  survival labels are unmeasurable no matter
                                  how much evidence exists, so the token can
                                  never reach `final` and is re-selected every
                                  pass until it decays into RETENTION_LOST.
  NOT_YET_DUE                     the 24 h closing edge has not passed yet.
  ALREADY_FINAL                   reported for reconciliation only; by the
                                  backlog predicate these are not in it.
  NO_EVIDENCE_IN_ANY_HORIZON      typed "other": anchored, due, inside
                                  retention, but not one tick lands in any
                                  horizon window.

Each class also carries `cheap_predicate` — how a selector can identify it
WITHOUT loading a token's sources, which is what stops the reconciler spending
its budget re-deriving permanently unrecoverable rows.

Usage:
    .venv/bin/python scripts/crypto_backlog_partition.py \\
        --db-path /scratch/copy.db --out /scratch/backlog_partition.json

Not part of the pytest suite; imported by no application code.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DEPLOYMENT_MARKERS = ("projects/probability-arena/data", "/var/lib/", "/srv/")
_PRODUCTION_BASENAMES = ("probability_arena.db",)


def _refuse_non_scratch(path: Path) -> None:
    """Refuse anything that looks like a real deployment OR development
    database.

    A three-substring blacklist tuned to one host is not a guard: a developer's
    own `<repo>/data/probability_arena.db` matches none of those fragments. The
    filename check is what actually stops that case, and the repo-root check
    stops a scratch copy accidentally landing inside a checkout. This script is
    read-only (`mode=ro`), so the guard is defence in depth rather than the
    only thing standing between it and a real file — but every script in this
    set should refuse the same paths, or the stated safety property is not the
    implemented one.
    """
    s = str(path)
    for frag in _DEPLOYMENT_MARKERS:
        if frag in s:
            raise SystemExit(f"refusing a deployment-looking path: {path}")
    if path.name in _PRODUCTION_BASENAMES:
        raise SystemExit(
            f"refusing {path.name!r}: that is the production database "
            "filename. Copy it to a scratch name first."
        )
    repo_root = Path(__file__).resolve().parents[1]
    if repo_root in path.parents:
        raise SystemExit(f"refusing a path inside the repo checkout: {path}")


HORIZONS = (("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440))
HORIZON_TOLERANCE = 0.5
WINDOW_HOURS = 48
RETENTION_DAYS = 7
CLOSING_EDGE_MIN = 1440 * (1 + HORIZON_TOLERANCE)      # 2160 min = 36 h


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db-path", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    args = ap.parse_args()

    db = Path(args.db_path).resolve()
    _refuse_non_scratch(db)
    if not db.exists():
        raise SystemExit(f"no such database: {db}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    now = datetime.now(timezone.utc)
    report: dict = {
        "db": str(db), "measured_at": now.isoformat(),
        "retention_days": args.retention_days,
        "window_hours": WINDOW_HOURS,
        "closing_edge_minutes": CLOSING_EDGE_MIN,
    }

    # ---------------------------------------------------------------- B4 ----
    arrivals = {}
    for label, days in (("24h", 1), ("3d", 3), ("7d", 7), ("14d", 14), ("30d", 30)):
        n = conn.execute(
            "SELECT count(*) FROM crypto_tokens WHERE chain='solana' "
            "AND first_seen_at >= ?", (_iso(now - timedelta(days=days)),)
        ).fetchone()[0]
        arrivals[label] = {"tokens": n, "per_day": round(n / days, 1)}
    per_day = conn.execute(
        "SELECT date(first_seen_at) d, count(*) FROM crypto_tokens "
        "WHERE chain='solana' GROUP BY d ORDER BY d"
    ).fetchall()
    # complete UTC days only: drop the first and last partial buckets
    complete = [c for _, c in per_day[1:-1]]
    complete_sorted = sorted(complete)

    def pctl(v, p):
        return v[min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))] if v else None

    report["b4_arrivals"] = {
        "windows": arrivals,
        "per_utc_day": {
            "complete_days": len(complete),
            "first_day": per_day[0][0] if per_day else None,
            "last_day": per_day[-1][0] if per_day else None,
            "min": min(complete) if complete else None,
            "p50": pctl(complete_sorted, 0.5),
            "p90": pctl(complete_sorted, 0.90),
            "p95": pctl(complete_sorted, 0.95),
            "max": max(complete) if complete else None,
            "mean": round(sum(complete) / len(complete), 1) if complete else None,
        },
        "last_14_complete_days": [
            {"day": d, "tokens": c} for d, c in per_day[-15:-1]],
        "note": (
            "The rate is non-stationary — it rises as the window shortens. A "
            "stationary p95 over the whole history therefore UNDERSTATES the "
            "current regime; the recent-14-day p95 is reported alongside it."
        ),
    }
    recent = sorted(c for _, c in per_day[-15:-1])
    report["b4_arrivals"]["recent_14d"] = {
        "p50": pctl(recent, 0.5), "p95": pctl(recent, 0.95),
        "max": max(recent) if recent else None,
        "mean": round(sum(recent) / len(recent), 1) if recent else None,
    }

    # ---------------------------------------------------------------- B5 ----
    cutoff = _iso(now - timedelta(hours=WINDOW_HOURS))
    retention_cutoff = now - timedelta(days=args.retention_days)

    totals = {
        "tokens_total": conn.execute(
            "SELECT count(*) FROM crypto_tokens WHERE chain='solana'").fetchone()[0],
        "outcomes_total": conn.execute(
            "SELECT count(*) FROM crypto_token_survival_outcomes").fetchone()[0],
        "outcomes_final": conn.execute(
            "SELECT count(*) FROM crypto_token_survival_outcomes WHERE final=1"
        ).fetchone()[0],
        "birth_events": conn.execute(
            "SELECT count(*) FROM crypto_token_birth_events").fetchone()[0],
    }

    # anchor per backlog token. `first_evidence_at` when a birth event exists,
    # otherwise the same min() compute_survival/build_birth_event would derive.
    rows = conn.execute(
        """
        SELECT t.token_address, t.first_seen_at,
               b.first_evidence_at, b.initial_liquidity_usd,
               (SELECT min(e.observed_at) FROM crypto_token_discovery_events e
                 WHERE e.chain='solana' AND e.token_address=t.token_address),
               (SELECT min(k.observed_at) FROM crypto_price_ticks k
                 WHERE k.chain='solana' AND k.token_address=t.token_address)
        FROM crypto_tokens t
        LEFT JOIN crypto_token_survival_outcomes o
               ON o.token_address = t.token_address AND o.chain = t.chain
        LEFT JOIN crypto_token_birth_events b
               ON b.token_address = t.token_address AND b.chain = t.chain
        WHERE t.chain='solana' AND t.first_seen_at < ?
          AND (o.id IS NULL OR o.final = 0)
        """, (cutoff,)
    ).fetchall()

    def parse(s):
        if not s:
            return None
        s = str(s).replace("T", " ").split("+")[0].split(".")[0]
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    anchors: dict[str, datetime] = {}
    liquidity_known: dict[str, bool] = {}
    no_anchor: list[str] = []
    for addr, first_seen, first_ev, init_liq, first_disc, first_tick in rows:
        cand = [parse(x) for x in (first_ev, first_seen, first_disc, first_tick)]
        cand = [c for c in cand if c is not None]
        a = parse(first_ev) or (min(cand) if cand else None)
        if a is None:
            no_anchor.append(addr)
            continue
        anchors[addr] = a
        liquidity_known[addr] = init_liq is not None

    # Which tokens have a tick inside each horizon's tolerance window? One
    # indexed pass per horizon over crypto_price_ticks, driven by a temp table
    # of per-token anchors (never 12k separate round trips).
    conn.execute("ATTACH DATABASE ':memory:' AS mem")
    conn.execute("CREATE TABLE mem.anchor (addr TEXT PRIMARY KEY, a TEXT)")
    conn.executemany("INSERT INTO mem.anchor VALUES (?,?)",
                     [(k, _iso(v)) for k, v in anchors.items()])
    conn.commit()

    horizon_hits: dict[str, set[str]] = {}
    for label, minutes in HORIZONS:
        tol = minutes * HORIZON_TOLERANCE
        hits = conn.execute(
            f"""
            SELECT DISTINCT m.addr
            FROM mem.anchor m
            JOIN crypto_price_ticks k
              ON k.chain='solana' AND k.token_address = m.addr
            WHERE k.observed_at > m.a
              AND k.observed_at >= datetime(m.a, '+{minutes - tol} minutes')
              AND k.observed_at <= datetime(m.a, '+{minutes + tol} minutes')
              AND k.liquidity_usd IS NOT NULL
            """
        ).fetchall()
        horizon_hits[label] = {r[0] for r in hits}

    any_tick = {r[0] for r in conn.execute(
        "SELECT DISTINCT m.addr FROM mem.anchor m JOIN crypto_price_ticks k "
        "ON k.chain='solana' AND k.token_address = m.addr").fetchall()}

    classes: dict[str, list[str]] = {
        "RETENTION_LOST": [], "RECOVERABLE_NOW": [], "PARTIALLY_RECOVERABLE": [],
        "MISSING_REQUIRED_INITIAL_STATE": [], "NOT_YET_DUE": [],
        "NO_EVIDENCE_IN_ANY_HORIZON": [],
    }
    classes["MISSING_REQUIRED_INITIAL_STATE"].extend(no_anchor)

    for addr, a in anchors.items():
        closing_edge = a + timedelta(minutes=CLOSING_EDGE_MIN)
        if now < closing_edge:
            classes["NOT_YET_DUE"].append(addr)
            continue
        if closing_edge < retention_cutoff:
            classes["RETENTION_LOST"].append(addr)
            continue
        if not liquidity_known.get(addr, False):
            classes["MISSING_REQUIRED_INITIAL_STATE"].append(addr)
            continue
        if addr in horizon_hits["24h"]:
            classes["RECOVERABLE_NOW"].append(addr)
        elif any(addr in horizon_hits[h] for h, _ in HORIZONS[:-1]):
            classes["PARTIALLY_RECOVERABLE"].append(addr)
        else:
            classes["NO_EVIDENCE_IN_ANY_HORIZON"].append(addr)

    backlog_n = len(rows)
    cheap = {
        "RETENTION_LOST": (
            "pure timestamp range on crypto_tokens.first_seen_at: anchor "
            f"< now - ({args.retention_days}d + {CLOSING_EDGE_MIN}min). No "
            "source load, no join — one indexable predicate, and it is "
            "MONOTONE (a token never leaves this class), so it can be written "
            "off once and excluded from selection forever."),
        "NOT_YET_DUE": (
            "first_seen_at > now - closing_edge; already implemented as "
            "`max_first_seen_at`/`min_age_minutes` on _universe."),
        "MISSING_REQUIRED_INITIAL_STATE": (
            "crypto_token_birth_events.initial_liquidity_usd IS NULL (or no "
            "birth row and no first-evidence timestamp) — one indexed lookup "
            "on the birth table, no tick scan. Also monotone in practice: the "
            "initial state is a property of a moment that has already passed."),
        "RECOVERABLE_NOW / PARTIALLY_RECOVERABLE / NO_EVIDENCE_IN_ANY_HORIZON": (
            "these three CANNOT be told apart without touching "
            "crypto_price_ticks, but the tick probe is a single EXISTS query "
            "on ix_crypto_price_ticks_token_address (~0.01 ms post-ANALYZE, "
            "~13 ms pre-ANALYZE) — cheap only once the query plan is fixed."),
    }
    report["b5_backlog"] = {
        "totals": totals,
        "backlog_size": backlog_n,
        "anchored": len(anchors),
        "classes": {k: len(v) for k, v in classes.items()},
        "class_share": {k: (round(100 * len(v) / backlog_n, 2) if backlog_n else 0)
                        for k, v in classes.items()},
        "sum_check": sum(len(v) for v in classes.values()),
        "cheap_identification": cheap,
        "horizon_hit_counts": {k: len(v) for k, v in horizon_hits.items()},
        "backlog_tokens_with_any_tick": len(any_tick),
        "oldest_backlog_first_seen_at": min(
            (str(r[1]) for r in rows), default=None),
    }

    # --------------------------------------------------------------- B5.3 ---
    # Is the missing 24h evidence PRUNED, or was it never RECORDED? Retention
    # can only be the cause for tokens whose evidence window has aged past it.
    # So slice by age: the 36h-3d cohort's entire 24h window (anchor+12h ..
    # anchor+36h) is at most 3 days old, i.e. comfortably inside a 7-day
    # retention — if an observation was ever written for those tokens, it is
    # still on disk. Whatever coverage that cohort shows is a pure measure of
    # what the observation lane recorded, with retention held out.
    cohorts = []
    for lo_h, hi_h, name in ((36, 72, "36h-3d_zero_pruning_possible"),
                             (72, 168, "3d-7d"),
                             (168, 204, "7d-8.5d_24h_window_partly_pruned")):
        conn.execute("DROP TABLE IF EXISTS mem.c")
        conn.execute(
            "CREATE TABLE mem.c AS SELECT token_address addr, first_seen_at fs "
            "FROM crypto_tokens WHERE chain='solana' "
            f"AND first_seen_at <= datetime('now','-{lo_h} hours') "
            f"AND first_seen_at >  datetime('now','-{hi_h} hours')")
        n = conn.execute("SELECT count(*) FROM mem.c").fetchone()[0]
        hits = {}
        for label, minutes in HORIZONS:
            tol = minutes * HORIZON_TOLERANCE
            hits[label] = conn.execute(
                "SELECT count(DISTINCT c.addr) FROM mem.c c "
                "JOIN crypto_price_ticks k ON k.chain='solana' "
                "  AND k.token_address = c.addr "
                f"WHERE k.observed_at >= datetime(c.fs,'+{minutes - tol} minutes') "
                f"  AND k.observed_at <= datetime(c.fs,'+{minutes + tol} minutes')"
            ).fetchone()[0]
        liq = conn.execute(
            "SELECT count(*) FROM mem.c c JOIN crypto_token_birth_events b "
            "ON b.chain='solana' AND b.token_address = c.addr "
            "WHERE b.initial_liquidity_usd IS NOT NULL").fetchone()[0]
        cohorts.append({
            "cohort": name, "age_hours": [lo_h, hi_h], "tokens": n,
            "horizon_hits": hits,
            "horizon_share_pct": {k: (round(100 * v / n, 1) if n else None)
                                  for k, v in hits.items()},
            "initial_liquidity_known": liq,
            "initial_liquidity_share_pct": (round(100 * liq / n, 1) if n else None),
        })

    # How long does the observation lane actually watch a token? First evidence
    # to the LAST tick it ever wrote, for every token that has any tick.
    conn.execute("DROP TABLE IF EXISTS mem.c")
    conn.execute(
        "CREATE TABLE mem.c AS SELECT token_address addr, first_seen_at fs "
        "FROM crypto_tokens WHERE chain='solana' "
        "AND first_seen_at <= datetime('now','-36 hours') "
        "AND first_seen_at >  datetime('now','-204 hours')")
    spans = [r[0] for r in conn.execute(
        "SELECT (julianday(max(k.observed_at)) - julianday(c.fs)) * 1440 "
        "FROM mem.c c JOIN crypto_price_ticks k "
        "  ON k.chain='solana' AND k.token_address = c.addr "
        "GROUP BY c.addr").fetchall() if r[0] is not None]
    spans.sort()
    birth_liq = conn.execute(
        "SELECT (initial_liquidity_usd IS NULL), count(*) "
        "FROM crypto_token_birth_events GROUP BY 1").fetchall()
    report["b5_3_observation_coverage"] = {
        "cohorts": cohorts,
        "observation_span_minutes": {
            "tokens_with_any_tick": len(spans),
            "p50": round(pctl(spans, 0.5), 1) if spans else None,
            "p90": round(pctl(spans, 0.90), 1) if spans else None,
            "max": round(max(spans), 1) if spans else None,
        },
        "birth_initial_liquidity": {
            "not_null": next((c for null, c in birth_liq if null == 0), 0),
            "null": next((c for null, c in birth_liq if null == 1), 0),
        },
        "note": (
            "The 36h-3d cohort cannot have lost anything to retention, so its "
            "horizon coverage measures ONLY what the observation lane wrote. "
            "If 24h coverage is low there, the evidence was never recorded and "
            "no retention window recovers it."
        ),
    }

    # ---------------------------------------------------------------- B7 ----
    # Retention model. Retention length does not act on the PAST: rows already
    # pruned are gone, so no window change recovers a single byte of the
    # existing backlog. What it buys is a longer DEADLINE for future tokens —
    # a token stays recoverable until age = closing_edge + retention_days.
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    tick_days = conn.execute(
        "SELECT date(observed_at) d, count(*) FROM crypto_price_ticks "
        "GROUP BY d ORDER BY d").fetchall()
    complete_tick_days = [c for _, c in tick_days[1:-1]]
    tick_rows = conn.execute(
        "SELECT count(*) FROM crypto_price_ticks").fetchone()[0]
    try:
        tick_bytes = conn.execute(
            "SELECT sum(pgsize) FROM dbstat WHERE name LIKE 'crypto_price_ticks%'"
        ).fetchone()[0] or 0
    except sqlite3.Error:
        tick_bytes = None
    per_row = (tick_bytes / tick_rows) if (tick_bytes and tick_rows) else None
    daily_rows = (sum(complete_tick_days) / len(complete_tick_days)
                  if complete_tick_days else None)

    windows = {}
    for r in (7, 10, 14, 21):
        extra_days = r - args.retention_days
        extra_rows = (daily_rows * extra_days) if daily_rows is not None else None
        extra_bytes = (extra_rows * per_row) if (extra_rows is not None and per_row) else None
        # how many CURRENT backlog tokens would still be inside retention at
        # this window — i.e. what the window would be worth if the evidence
        # had never been pruned. It is a counterfactual ceiling, not a
        # recovery forecast.
        would_be_inside = sum(
            1 for a in anchors.values()
            if (a + timedelta(minutes=CLOSING_EDGE_MIN)) >= now - timedelta(days=r)
        )
        windows[f"{r}d"] = {
            "recovery_deadline_age_days": round(CLOSING_EDGE_MIN / 1440 + r, 2),
            "extra_days_vs_current": extra_days,
            "incremental_tick_rows": (round(extra_rows) if extra_rows is not None else None),
            "incremental_bytes": (round(extra_bytes) if extra_bytes is not None else None),
            "incremental_mib": (round(extra_bytes / 1048576, 1)
                                if extra_bytes is not None else None),
            "absorbed_by_existing_freelist": (
                bool(extra_bytes is not None and extra_bytes <= freelist * page_size)),
            "counterfactual_backlog_inside_window": would_be_inside,
        }
    report["b7_retention"] = {
        "page_size": page_size, "page_count": page_count,
        "freelist_pages": freelist,
        "freelist_bytes": freelist * page_size,
        "freelist_share": round(freelist / page_count, 4) if page_count else None,
        "crypto_price_ticks_rows": tick_rows,
        "crypto_price_ticks_bytes": tick_bytes,
        "bytes_per_tick_row": round(per_row, 1) if per_row else None,
        "tick_rows_per_complete_day": round(daily_rows, 1) if daily_rows else None,
        "tick_span": [tick_days[0][0], tick_days[-1][0]] if tick_days else None,
        "windows": windows,
        "note": (
            "Retention is NOT retroactive: rows already pruned cannot be "
            "recovered by lengthening the window, so the counterfactual "
            "column is a ceiling on what a LONGER WINDOW WOULD HAVE SAVED, "
            "never a forecast of what extending it now recovers. And because "
            "SQLite DELETE never shrinks the file, incremental rows are "
            "absorbed by the existing freelist until they exceed it — so a "
            "longer window is close to free in disk terms while the freelist "
            "holds, and shortening it frees no disk at all."
        ),
    }

    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
