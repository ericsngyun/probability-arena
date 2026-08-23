"""MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001 — the session runner.

Runs one prospective session under the frozen sampling contract
(`MARKET-MICROSTRUCTURE-EDGE-001` Amendment 2 + capture plan §1–§3) and emits
the eligibility-transition audit.

**The collector is not modified.** Subscriptions are fixed for the session and
bounded by the never-exceed concurrency ceiling — that is the capacity guard.
The research panel of K=12 rotates every 300 s from lagged sequenced
order-book activity among the subscribed set, which is the quantity Amendment 2
actually freezes. This split is why no mid-session resubscription is needed.

Read-only. `--mode validation` marks the tape `VALIDATION_ONLY` and stamps
every row `dataset_role=VALIDATION`, so it can never be mistaken for
confirmation data.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.microstructure.panel import (  # noqa: E402
    DatasetRole, HARD_STOP_FPS, MarketMeta, NEVER_EXCEED_CONCURRENCY,
    Observation, PANEL_K, PanelSession, RowProvenance, SESSION_OK,
    assert_capacity_relationship, evaluate_safety_stop,
)

FEATURE_SCHEMA_VERSION = "microstructure-panel-v1"
PREREGISTRATION_VERSION = "MARKET-MICROSTRUCTURE-EDGE-001 Amendment 2"


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def git_commit() -> str:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()


def read_tape(env_dir: Path):
    """Every archived frame, in segment then file order."""
    for d in sorted(env_dir.glob("**/segment=*")):
        f = d / "events.jsonl.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except Exception:
                    continue


def to_observation(rec: dict) -> Observation | None:
    ts = rec.get("received_at_utc")
    if not ts:
        return None
    return Observation(
        market_ticker=rec.get("market_ticker"),
        received_at_utc=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        message_type=rec.get("message_type") or "",
        sid=rec.get("subscription_id"),
        seq=rec.get("seq"),
        subscription_generation=rec.get("subscription_generation"),
    )


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--mode", choices=["validation", "confirmation"],
                    required=True)
    ap.add_argument("--markets-file", required=True,
                    help="candidate tickers, one per line; <= the ceiling")
    ap.add_argument("--events-file", required=True,
                    help="JSON {ticker: {series, occurrence_datetime}}")
    ap.add_argument("--seconds", type=int, required=True)
    ap.add_argument("--max-events", type=int, default=40_000_000)
    ap.add_argument("--k", type=int, default=PANEL_K)
    ap.add_argument("--root-base", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])

    # -- preflight: refuse startup rather than discover the problem later ----
    assert_capacity_relationship(a.max_events, HARD_STOP_FPS, a.seconds)
    log(f"capacity relationship OK: max_events={a.max_events:,} > "
        f"{HARD_STOP_FPS:,} x {a.seconds:,} = {HARD_STOP_FPS * a.seconds:,}")

    tickers = [t for t in Path(a.markets_file).read_text().split() if t]
    if len(tickers) > NEVER_EXCEED_CONCURRENCY:
        log(f"REFUSED: {len(tickers)} subscriptions exceeds the never-exceed "
            f"ceiling {NEVER_EXCEED_CONCURRENCY}")
        return 2
    events = json.loads(Path(a.events_file).read_text())
    missing = [t for t in tickers if t not in events]
    if missing:
        log(f"REFUSED: no event time for {missing[:3]}")
        return 2
    log(f"{len(tickers)} subscriptions (ceiling {NEVER_EXCEED_CONCURRENCY}), "
        f"research panel K={a.k}")

    root = Path(a.root_base) / f"session={a.label}"
    if root.exists() and any(root.iterdir()):
        log(f"REFUSED: {root} exists and is not empty; a session owns its root")
        return 2
    root.mkdir(parents=True, exist_ok=True)

    from app.realtime import archive_head, session_root
    archive_head.initialize_archive(root, "production",
                                    archive_identity="kalshi-realtime")
    session_id = session_root.new_session_id()
    session_root.claim_session_root(root, "production", session_id=session_id)
    if a.mode == "validation":
        (root / "VALIDATION_ONLY").write_text(
            "This tape is VALIDATION_ONLY and is permanently excluded from "
            "MARKET-MICROSTRUCTURE-EDGE-001 confirmation.\n")

    mk_file = root / "subscribed.txt"
    mk_file.write_text("\n".join(tickers))

    started = datetime.now(timezone.utc)
    cap_out = root / "capture.json"
    cmd = [sys.executable, str(REPO / "scripts" / "kalshi_prod_capture_p4.py"),
           "capture", "--archive-root", str(root),
           "--tickers-file", str(mk_file),
           "--channels", "orderbook_delta,trade,ticker",
           "--max-seconds", str(a.seconds),
           "--max-events", str(a.max_events),
           "--label", a.label, "--out", str(cap_out)]
    log(f"capture starting: {a.seconds}s")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log(f"capture finished rc={proc.returncode}")
    if proc.returncode != 0:
        log((proc.stderr or "")[-1500:])

    cap = json.loads(cap_out.read_text()) if cap_out.exists() else {}
    sr = cap.get("session_result", {})

    # -- replay the tape through the frozen decision core --------------------
    markets = {t: MarketMeta(
        ticker=t, series=events[t]["series"],
        occurrence_datetime=datetime.fromisoformat(
            events[t]["occurrence_datetime"].replace("Z", "+00:00")))
        for t in tickers}

    frames = [o for o in (to_observation(r)
                          for r in read_tape(root / "env=production"))
              if o is not None]
    frames.sort(key=lambda o: o.received_at_utc)
    if not frames:
        log("REFUSED: no frames on the tape")
        return 3
    session_open = frames[0].received_at_utc
    session_end = frames[-1].received_at_utc
    log(f"tape: {len(frames):,} frames, {session_open} -> {session_end}")

    ps = PanelSession(session_open=session_open, markets=markets, k=a.k)
    ticks = ps.decision_ticks(session_end)
    log(f"{len(ticks)} decision ticks (warmup {session_open} + 300s)")

    decisions, cursor = [], 0
    for t in ticks:
        while cursor < len(frames) and frames[cursor].received_at_utc <= t:
            ps.observe(frames[cursor])
            cursor += 1
        decisions.append(ps.decide_panel(t))
        log(f"  tick {t.isoformat()}  panel={len(decisions[-1].panel)} "
            f"{list(decisions[-1].panel)[:4]}{'...' if len(decisions[-1].panel) > 4 else ''}")

    peak = ((cap.get("metrics") or {}).get("rate") or {}).get("peak_1s_sliding")
    if peak is None:
        peak = _peak_1s_sliding([o.received_at_utc for o in frames])
    safety = evaluate_safety_stop(peak, at=session_end)
    log(f"safety: peak_1s_sliding={peak} vs {HARD_STOP_FPS} -> {safety.status}")

    role = (DatasetRole.VALIDATION if a.mode == "validation"
            else DatasetRole.CONFIRMATION)
    commit = git_commit()
    provenance = [
        RowProvenance(
            session_id=session_id, panel_tick=d.tick_t, market=m,
            subscription_generation=ps._books.subscription_generation,
            feature_schema_version=FEATURE_SCHEMA_VERSION,
            capture_commit=commit,
            preregistration_version=PREREGISTRATION_VERSION,
            dataset_role=role, session_status=safety.status).to_dict()
        for d in decisions for m in d.panel]

    result = {
        "milestone": "MARKET-MICROSTRUCTURE-CAPTURE-RUNNER-VALIDATION-001",
        "label": a.label, "mode": a.mode, "dataset_role": role,
        "validation_only": a.mode == "validation",
        "session_id": session_id, "capture_commit": commit,
        "preregistration_version": PREREGISTRATION_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "archive_root": str(root),
        "started_at": started.isoformat(),
        "session_open": session_open.isoformat(),
        "session_end": session_end.isoformat(),
        "subscribed": tickers, "subscribed_count": len(tickers),
        "never_exceed_concurrency": NEVER_EXCEED_CONCURRENCY,
        "panel_k": a.k,
        "capacity_relationship": {
            "max_events": a.max_events, "hard_stop_fps": HARD_STOP_FPS,
            "max_seconds": a.seconds,
            "required_greater_than": HARD_STOP_FPS * a.seconds,
            "holds": a.max_events > HARD_STOP_FPS * a.seconds},
        "safety": safety.to_dict(),
        "capture_session_status": sr.get("status"),
        "capture_returncode": proc.returncode,
        "frames": len(frames),
        "session_result": sr,
        "decision_ticks": [d.to_dict() for d in decisions],
        "row_provenance_sample": provenance[:20],
        "research_rows_emitted": len(provenance),
        "usable_as_confirmation": (role == DatasetRole.CONFIRMATION
                                   and safety.status == SESSION_OK),
    }
    Path(a.out).write_text(json.dumps(result, indent=2, default=str))
    log(f"wrote {a.out}")
    return 0


def _peak_1s_sliding(times) -> int:
    """Sliding 1 s maximum — the primary capacity statistic (Amendment 1)."""
    ns = sorted(int(t.timestamp() * 1e9) for t in times)
    peak, lo = 0, 0
    for hi in range(len(ns)):
        while ns[hi] - ns[lo] >= 1_000_000_000:
            lo += 1
        peak = max(peak, hi - lo + 1)
    return peak


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
