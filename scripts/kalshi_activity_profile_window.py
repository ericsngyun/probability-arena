"""PROD-ACTIVITY-PROFILE-001 — run one window and gate it.

Wraps the FROZEN capture path. This script opens no socket of its own and edits
no collector code: it prepares an immutable per-window archive root, records
host load around the capture, invokes `kalshi_prod_capture_p4.py`, then measures
the resulting tape and evaluates the s6 hard stop.

The 3,500 f/s stop is evaluated HERE, on completion, before the next window is
permitted to start — deliberately not in-flight, because an in-flight rate
governor would mean editing the collector and the collector is frozen.

`peak_1s_sliding` is the sole gating statistic. `peak_1s_calendar_bucket` is
recorded as a diagnostic and gates nothing.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

HARD_STOP_FPS = 3500
NS = 1_000_000_000
REPO = Path(__file__).resolve().parents[1]


def host_load() -> dict:
    """Venue intensity and host contention are different things."""
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        one = five = fifteen = None
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = v.strip()
    except OSError:
        pass
    st = os.statvfs("/")
    return {"loadavg_1m": one, "loadavg_5m": five, "loadavg_15m": fifteen,
            "cpu_count": os.cpu_count(),
            "meminfo": mem,
            "disk_free_gb": round(st.f_bavail * st.f_frsize / 2**30, 2),
            "at_utc": datetime.now(timezone.utc).isoformat()}


def _sliding_peak_1s(ts):
    ts = sorted(ts)
    if not ts:
        return None
    best, j = 0, 0
    for i, t in enumerate(ts):
        while j < len(ts) and ts[j] < t + NS:
            j += 1
        best = max(best, j - i)
    return best


def _sliding_series(ts):
    """Rate in every 1 s window anchored at a frame — the distribution the
    peak is the maximum of. Reported as a DIAGNOSTIC: p95/p99 were not
    preregistered and gate nothing."""
    ts = sorted(ts)
    out, j = [], 0
    for i, t in enumerate(ts):
        while j < len(ts) and ts[j] < t + NS:
            j += 1
        out.append(j - i)
    return out


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[k]


def measure_tape(env_dir: Path) -> dict:
    """Per-market and per-channel wire activity, straight off the tape."""
    per_market = defaultdict(Counter)
    by_type, by_sid = Counter(), Counter()
    ts_all, ts_by_sid = [], defaultdict(list)
    seq_by_sid = defaultdict(list)
    segments = 0

    seg_dirs = sorted(env_dir.glob("**/segment=*"))
    segments = len(seg_dirs)
    for d in seg_dirs:
        f = d / "events.jsonl.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                mt = r.get("message_type")
                by_type[mt] += 1
                sid = r.get("subscription_id")
                by_sid[str(sid)] += 1
                t = r.get("received_at_utc")
                if t:
                    ns = int(datetime.fromisoformat(
                        t.replace("Z", "+00:00")).timestamp() * 1e9)
                    ts_all.append(ns)
                    ts_by_sid[str(sid)].append(ns)
                tk = r.get("market_ticker")
                if tk:
                    per_market[tk][mt] += 1
                if r.get("seq") is not None:
                    seq_by_sid[str(sid)].append(r["seq"])

    span = (max(ts_all) - min(ts_all)) / NS if len(ts_all) > 1 else 0.0
    series = _sliding_series(ts_all)
    cal = Counter(t // NS for t in ts_all)

    seq_report = {}
    for sid, seqs in seq_by_sid.items():
        s = sorted(seqs)
        seq_report[sid] = {
            "frames": len(s), "min": s[0], "max": s[-1],
            "distinct": len(set(s)),
            "contiguous": len(set(s)) == len(s) and s[-1] - s[0] + 1 == len(s),
        }

    total = sum(by_type.values())
    markets = {}
    for tk, c in per_market.items():
        ob = c.get("orderbook_delta", 0) + c.get("orderbook_snapshot", 0)
        markets[tk] = {
            "orderbook_frames": ob,
            "trade_frames": c.get("trade", 0),
            "ticker_frames": c.get("ticker", 0),
            "total_frames": sum(c.values()),
            "share_of_venue_traffic": round(sum(c.values()) / total, 6) if total else None,
            "sequenced_event_rate_per_s": round(ob / span, 4) if span else None,
        }

    return {
        "frames": total,
        "span_seconds": round(span, 3),
        "segments_on_disk": segments,
        "by_message_type": dict(by_type),
        "by_sid": dict(by_sid),
        "sequence_per_sid": seq_report,
        "rate": {
            "mean_fps": round(total / span, 4) if span else None,
            "median_fps_sliding": _pct(series, 0.50),
            "p95_fps_sliding_DIAGNOSTIC": _pct(series, 0.95),
            "p99_fps_sliding_DIAGNOSTIC": _pct(series, 0.99),
            "peak_1s_sliding": _sliding_peak_1s(ts_all),
            "peak_1s_calendar_bucket": max(cal.values()) if cal else None,
            "silent_seconds": sum(1 for v in
                                  (cal.get(i, 0) for i in
                                   range(min(cal) if cal else 0,
                                         (max(cal) + 1) if cal else 0))
                                  if v == 0),
        },
        "markets": markets,
    }


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--day", required=True)
    ap.add_argument("--slot", required=True)
    ap.add_argument("--tickers-file", required=True)
    ap.add_argument("--seconds", type=int, default=1500)
    ap.add_argument("--channels", default="orderbook_delta,ticker,trade")
    ap.add_argument("--root-base", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])

    root = Path(a.root_base) / f"day={a.day}" / f"slot={a.slot}"
    if root.exists() and any(root.iterdir()):
        print(f"REFUSED: {root} already exists and is not empty. Each window "
              f"owns an immutable root; reusing one would mix two sessions.")
        return 2
    root.mkdir(parents=True, exist_ok=True)

    # The archive must be brought into existence BEFORE the collector runs.
    # It fails closed if it is not (`ArchiveNotInitializedError: no genesis
    # marker`), which is correct and is how this omission was caught -- the
    # capture reported `status: archive_error` while still exiting 0.
    from app.realtime import archive_head, session_root
    archive_head.initialize_archive(root, "production",
                                    archive_identity="kalshi-realtime")
    # B4: one archive root belongs to one session, claimed durably BEFORE the
    # socket opens. Now lands in `env=production/` -- the directory the archive
    # actually writes to -- since the P4.2 repair.
    session_id = session_root.new_session_id()
    claim = session_root.claim_session_root(root, "production",
                                            session_id=session_id)

    before = host_load()
    started = datetime.now(timezone.utc).isoformat()
    cap_out = root / "capture.json"
    cmd = [sys.executable, str(REPO / "scripts" / "kalshi_prod_capture_p4.py"),
           "capture", "--archive-root", str(root),
           "--tickers-file", a.tickers_file, "--channels", a.channels,
           "--max-seconds", str(a.seconds), "--label", a.label,
           "--out", str(cap_out)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    finished = datetime.now(timezone.utc).isoformat()
    after = host_load()

    env_dir = root / "env=production"
    tape = measure_tape(env_dir) if env_dir.exists() else {"frames": 0}

    peak = (tape.get("rate") or {}).get("peak_1s_sliding")
    breached = peak is not None and peak > HARD_STOP_FPS

    # A window that ran short of its budget is SEMANTICALLY DIFFERENT from one
    # that ran to completion, and is reported as such rather than pooled.
    span = tape.get("span_seconds") or 0
    short = span < a.seconds * 0.9
    # The capture exits 0 even when the SESSION failed, so its own status is
    # read rather than the process return code.
    # `capped_time` and `capped_events` are the NORMAL terminal statuses for a
    # bounded window -- the session stopped because it was TOLD to. Treating
    # them as failures would have marked all six windows invalid.
    cap_status = None
    if cap_out.exists():
        try:
            cap_status = json.loads(cap_out.read_text())[
                "session_result"].get("status")
        except Exception:
            cap_status = "UNREADABLE"

    validity = "VALID"
    if proc.returncode != 0:
        validity = f"INVALID:capture_exit_{proc.returncode}"
    elif cap_status in ("archive_error",):
        validity = f"INVALID:session_status_{cap_status}"
    elif cap_status == "capped_reconnects":
        # The session gave up reconnecting. It produced evidence, but not for
        # the window that was asked for.
        validity = "TERMINATED_EARLY:capped_reconnects"
    elif tape.get("frames", 0) == 0:
        validity = "INVALID:no_frames"
    elif short:
        validity = f"TERMINATED_EARLY:span_{span:.0f}s_of_{a.seconds}s"

    result = {
        "milestone": "PROD-ACTIVITY-PROFILE-001", "phase": "window",
        "label": a.label, "profile_day_et": a.day, "slot": a.slot,
        "archive_root": str(root), "started_at": started,
        "finished_at": finished,
        "validity": validity,
        "host_load_before": before, "host_load_after": after,
        "capture_returncode": proc.returncode,
        "capture_session_status": cap_status,
        "session_id": session_id,
        "session_claim_digest": (claim.claim_digest
                                 if hasattr(claim, "claim_digest") else None),
        "capture_stderr_tail": proc.stderr[-2000:] if proc.stderr else "",
        "tape": tape,
        "hard_stop": {
            "threshold_fps": HARD_STOP_FPS,
            "statistic": "peak_1s_sliding",
            "observed": peak,
            "breached": breached,
            "action": ("HALT THE SET — halve the universe and restart all six "
                       "windows (s6)") if breached else "none",
        },
    }
    Path(a.out).write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    (root / "window.json").write_text(json.dumps(result, indent=2, sort_keys=True,
                                                 default=str))

    r = tape.get("rate") or {}
    print(f"  window            {a.label}  ({a.day} slot {a.slot})")
    print(f"  validity          {validity}")
    print(f"  frames            {tape.get('frames')} over {span:.0f}s")
    print(f"  mean              {r.get('mean_fps')} f/s")
    print(f"  peak_1s_sliding   {r.get('peak_1s_sliding')} f/s   "
          f"(calendar bucket {r.get('peak_1s_calendar_bucket')})")
    print(f"  hard stop {HARD_STOP_FPS} -> {'BREACHED' if breached else 'clear'}")
    print(f"  loadavg 1m        {before.get('loadavg_1m')} -> {after.get('loadavg_1m')}")
    return 1 if breached or validity.startswith("INVALID") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
