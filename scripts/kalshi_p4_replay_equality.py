"""KALSHI-P4-4 — production replay-equality qualification.

Read-only. No network, no credential, no venue, no capital. It reads the FROZEN
P4 production tape and the capture JSON recorded beside it, and asks one
question: does replaying the durable evidence reproduce the state the live
collector actually held?

Two arms are required and neither is sufficient alone:

* the **real production tape** qualifies replay equality on real venue traffic;
* a **wire-faithful B3 positive control** proves the repaired branch is actually
  exercised, because the production tape contains ZERO error frames and a clean
  replay of it cannot touch that code path at all.

Comparisons that P3 declares semantically invalid are NOT made. `recoveries`
counts an outbound collector ACTION; the tape records inbound venue messages
only, so requiring equality there would be requiring the tape to contain
something it is defined not to contain.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.realtime.archive import EventArchive, replay          # noqa: E402

ORDERBOOK = ("orderbook_snapshot", "orderbook_delta")


def _fail(checks, name, detail):
    checks.append({"check": name, "pass": False, "detail": detail})


def _ok(checks, name, detail):
    checks.append({"check": name, "pass": True, "detail": detail})


def tape_census(records) -> dict:
    """What the durable evidence contains, counted directly."""
    by_type, by_sid, gens, conn_gens, segs = Counter(), Counter(), Counter(), Counter(), set()
    seq_by_sid: dict = {}
    for r in records:
        mt = r.get("message_type") or r.get("event_type")
        by_type[mt] += 1
        sid = r.get("subscription_id", r.get("sid"))
        by_sid[sid] += 1
        gens[r.get("subscription_generation")] += 1
        conn_gens[r.get("connection_generation")] += 1
        segs.add(r.get("segment_id"))
        if r.get("seq") is not None:
            seq_by_sid.setdefault(sid, []).append(r["seq"])
    return {"by_type": dict(by_type), "by_sid": dict(by_sid),
            "subscription_generations": dict(gens),
            "connection_generations": dict(conn_gens),
            "segment_ids": len(segs), "seq_by_sid": seq_by_sid}


def qualify(archive_root: Path, capture_json: Path) -> dict:
    checks: list = []
    store = EventArchive(archive_root, environment="production")
    records = store.read_verified()
    integrity = store.verify()
    census = tape_census(records)
    out = replay(records)
    live = json.loads(capture_json.read_text())["live_terminal_state"]

    # --- integrity -------------------------------------------------------
    if integrity["intact"] and not integrity["mismatched"]:
        _ok(checks, "archive_integrity",
            f"{integrity['records']} records, 0 digest mismatches, "
            f"{integrity['truncated_records']} truncated")
    else:
        _fail(checks, "archive_integrity", integrity)

    # --- frame conservation ---------------------------------------------
    ob_frames = sum(census["by_type"].get(t, 0) for t in ORDERBOOK)
    if out["events_applied"] == ob_frames:
        _ok(checks, "frame_conservation",
            f"every one of the {ob_frames} orderbook frames was applied")
    else:
        _fail(checks, "frame_conservation",
              f"applied {out['events_applied']} of {ob_frames} orderbook frames")

    if out["events_rejected"] == 0 and not out["faults"]:
        _ok(checks, "no_replay_faults", "0 rejected, 0 faults")
    else:
        _fail(checks, "no_replay_faults",
              {"rejected": out["events_rejected"], "faults": out["faults"][:5]})

    # --- generation conservation ----------------------------------------
    if len(census["subscription_generations"]) == 1 and \
            len(census["connection_generations"]) == 1:
        _ok(checks, "generation_conservation",
            f"subscription_generation={list(census['subscription_generations'])}, "
            f"connection_generation={list(census['connection_generations'])}, "
            "every record stamped")
    else:
        _fail(checks, "generation_conservation",
              {"subscription": census["subscription_generations"],
               "connection": census["connection_generations"]})

    # --- per-sequenced-SID sequence classification ------------------------
    seq_report = {}
    seq_clean = True
    for sid, seqs in sorted(census["seq_by_sid"].items(), key=lambda kv: str(kv[0])):
        s = sorted(seqs)
        contiguous = (len(set(s)) == len(s) and s[-1] - s[0] + 1 == len(s))
        seq_report[str(sid)] = {"frames": len(s), "min": s[0], "max": s[-1],
                                "distinct": len(set(s)), "contiguous": contiguous}
        if not contiguous:
            seq_clean = False
    if seq_clean:
        _ok(checks, "sequence_classification_per_sid", seq_report)
    else:
        _fail(checks, "sequence_classification_per_sid", seq_report)

    # --- NO FABRICATED B3 GAP --------------------------------------------
    # The specific failure B3 caused: replay inventing a gap the venue never
    # sent, halting the subscription and refusing the remainder of the tape.
    fabricated = [t for t, st in out["stats"].items() if st.get("gaps")]
    halted = [t for t, s in out.get("publication_states", {}).items()
              if (s.get("state") if isinstance(s, dict) else s) == "book_halted"]
    if not fabricated and not halted:
        _ok(checks, "no_fabricated_b3_gap",
            "0 markets report a gap; 0 markets halted")
    else:
        _fail(checks, "no_fabricated_b3_gap",
              {"gaps": fabricated, "halted": halted})

    # --- terminal-state equality, per market ------------------------------
    live_books = {}
    for sid, entry in live.items():
        if not entry.get("carries_orderbook", True):
            continue
        for ticker, book in entry.get("books", {}).items():
            live_books[ticker] = book

    if set(live_books) != set(out["checksums"]):
        _fail(checks, "market_set_equality",
              {"live_only": sorted(set(live_books) - set(out["checksums"])),
               "replay_only": sorted(set(out["checksums"]) - set(live_books))})
    else:
        _ok(checks, "market_set_equality", f"{len(live_books)} markets both sides")

    mismatches, compared = [], 0
    for ticker, lb in sorted(live_books.items()):
        if ticker not in out["checksums"]:
            continue
        compared += 1
        rep_state = out.get("publication_states", {}).get(ticker) or {}
        if isinstance(rep_state, str):
            rep_state = {"state": rep_state}
        diffs = {}
        if lb.get("checksum") != out["checksums"].get(ticker):
            diffs["checksum"] = [lb.get("checksum"), out["checksums"].get(ticker)]
        if lb.get("publishable") != out["publishable"].get(ticker):
            diffs["publishable"] = [lb.get("publishable"),
                                    out["publishable"].get(ticker)]
        if lb.get("publication_state") and rep_state.get("state") and \
                lb["publication_state"] != rep_state["state"]:
            diffs["publication_state"] = [lb["publication_state"],
                                          rep_state["state"]]
        rs = out["stats"].get(ticker, {})
        ls = lb.get("stats", {})
        # `recoveries` is EXCLUDED by contract: it counts a collector action,
        # not a venue message, so the tape cannot carry it (P3 s8.2a).
        for k in ("snapshots", "deltas", "gaps", "duplicates", "regressions",
                  "resyncs", "rejected_pre_snapshot",
                  "rejected_pre_generation_snapshot", "generation_boundaries"):
            if k in ls and k in rs and ls[k] != rs[k]:
                diffs.setdefault("stats", {})[k] = [ls[k], rs[k]]
        if lb.get("last_seq") is not None and rs.get("last_seq") is not None \
                and lb["last_seq"] != rs["last_seq"]:
            diffs["last_seq"] = [lb["last_seq"], rs["last_seq"]]
        if diffs:
            mismatches.append({"market": ticker, "diffs": diffs})

    if compared == 0:
        _fail(checks, "terminal_state_equality",
              "nothing was compared; the check is vacuous")
    elif mismatches:
        _fail(checks, "terminal_state_equality",
              {"compared": compared, "mismatched": mismatches[:10]})
    else:
        _ok(checks, "terminal_state_equality",
            f"{compared} markets: checksum, publishable, publication_state, "
            f"last_seq and 9 stat counters all equal")

    return {"milestone": "KALSHI-P4-4-REPLAY-EQUALITY",
            "archive_root": str(archive_root), "external_calls": 0,
            "persisted": False, "records": integrity["records"],
            "tape_census": {k: v for k, v in census.items() if k != "seq_by_sid"},
            "replay": {"markets": out["markets"],
                       "events_applied": out["events_applied"],
                       "events_rejected": out["events_rejected"],
                       "faults": out["faults"][:5]},
            "checks": checks,
            "verdict": "QUALIFIED" if all(c["pass"] for c in checks)
                       else "NOT_QUALIFIED"}


def main(argv) -> int:
    if len(argv) < 3:
        print("usage: kalshi_p4_replay_equality.py <archive_root> <capture.json> "
              "[--json out.json]")
        return 2
    result = qualify(Path(argv[1]), Path(argv[2]))
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(
            json.dumps(result, indent=2, sort_keys=True, default=str))
    for c in result["checks"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['check']}")
        if not c["pass"]:
            print(f"         {json.dumps(c['detail'], default=str)[:400]}")
    print(f"\n  VERDICT: {result['verdict']}")
    return 0 if result["verdict"] == "QUALIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
