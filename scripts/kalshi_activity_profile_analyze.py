"""PROD-ACTIVITY-PROFILE-001 — the analysis stage, run exactly as Amendment 3 froze it.

Read-only over the six completed window artifacts. Amendment 3 specified these
statistics BEFORE any window existed; this tool implements that section and
nothing else. It adds no statistic that was not preregistered, and it does not
select a panel -- Amendment 3 deliberately leaves the panel-selection rule to a
separate preregistration written once these measurements exist.

Amendment 4 governs the one right-censored window (day 2 slot C, `capped_events`
at exactly 1,000,000 frames, 1472.3s of a 1500s budget):
  * exposure is that window's own `session_duration_s`, never the budget;
  * additive totals from it are never pooled as equal-duration totals;
  * its peak is reported as a LOWER BOUND and never extrapolated.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from pathlib import Path

DAYS = ("2026-08-20", "2026-08-21")
SLOTS = ("A", "B", "C")
HARD_STOP_FPS = 3500
CLOSER_ENVELOPE_FPS = 6900
MAX_SEGMENT_RECORDS = 13000
S4_MIN_DELTAS = 500
S4_MIN_SNAPSHOTS = 1


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _avg_ranks(vals: list[float]) -> list[float]:
    """Average ranks, so ties cannot manufacture or destroy correlation."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rho = Pearson on average ranks. None when undefined."""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    if dx <= 0 or dy <= 0:
        return None
    return round(num / (dx * dy) ** 0.5, 4)


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def load(base: Path) -> dict:
    """Every window artifact, plus the segment manifests behind it."""
    out = {}
    for day in DAYS:
        for slot in SLOTS:
            w = json.loads((base / f"day={day}" / f"window-{slot}.json").read_text())
            root = Path(w["archive_root"])
            cap = json.loads((root / "capture.json").read_text())
            segs = []
            for m in sorted((root / "env=production").glob("segment=*/manifest.json")):
                segs.append(json.loads(m.read_text()))
            out[(day, slot)] = {"window": w, "capture": cap, "segments": segs}
    return out


def decision_a(data: dict) -> dict:
    """A — is production capacity healthy enough to leave the collector FROZEN?"""
    rows, worst = [], {}
    for key in sorted(data):
        d = data[key]
        w, sr = d["window"], d["capture"]["session_result"]
        rate = w["tape"]["rate"]
        censored = sr["status"] == "capped_events"
        segs = d["segments"]
        closes = [
            (_ts(s["closed_at"]) - _ts(s["opened_at"])).total_seconds()
            for s in segs if s.get("closed_at") and s.get("opened_at")
        ]
        rows.append({
            "window": f"{key[0]} {key[1]}",
            "censored": censored,
            "peak_1s_sliding": rate["peak_1s_sliding"],
            "peak_is_lower_bound": censored,
            "mean_fps_over_observed": round(
                w["tape"]["frames"] / w["session_duration_s"], 3),
            "exposure_s": round(w["session_duration_s"], 1),
            "segments_committed": sr["segments_committed"],
            "segments_on_disk": w["tape"]["segments_on_disk"],
            "max_records_per_segment": max((s["record_count"] for s in segs),
                                           default=None),
            "segment_close_status": sorted({s.get("close_status") for s in segs}),
            "segment_open_to_close_s": [round(c, 1) for c in closes],
            "rotation_failures": sr["rotation_failures"],
            "sequence_faults": sr["sequence_faults"],
            "frames_malformed": sr["frames_malformed"],
            "events_rejected": sr["events_rejected"],
            "events_received": sr["events_received"],
            "events_archived": sr["events_archived"],
            "append_complete": sr["events_received"] == sr["events_archived"],
            "reconnects": sr["reconnects"],
            "recoveries_requested": sr["recoveries_requested"],
            "markets_subscribed": sr["markets_subscribed"],
            "loadavg_1m_before": w["host_load_before"]["loadavg_1m"],
            "loadavg_1m_after": w["host_load_after"]["loadavg_1m"],
            "disk_free_gb_after": w["host_load_after"]["disk_free_gb"],
        })
    peaks = [(r["peak_1s_sliding"], r["window"], r["censored"]) for r in rows]
    mx = max(peaks)
    worst = {"peak": mx[0], "window": mx[1], "is_lower_bound": mx[2]}
    over_cap = [r["window"] for r in rows
                if (r["max_records_per_segment"] or 0) > MAX_SEGMENT_RECORDS]
    return {
        "rows": rows,
        "max_peak_1s_sliding": worst,
        "hard_stop_fps": HARD_STOP_FPS,
        "closer_envelope_fps": CLOSER_ENVELOPE_FPS,
        "headroom_vs_stop": round(HARD_STOP_FPS / worst["peak"], 2),
        "headroom_vs_envelope": round(CLOSER_ENVELOPE_FPS / worst["peak"], 2),
        "any_observed_breach": any(r["peak_1s_sliding"] > HARD_STOP_FPS
                                   for r in rows),
        "segments_over_13000": over_cap,
        "total_rotation_failures": sum(r["rotation_failures"] for r in rows),
        "total_sequence_faults": sum(r["sequence_faults"] for r in rows),
        "total_frames_malformed": sum(r["frames_malformed"] for r in rows),
        "total_events_rejected": sum(r["events_rejected"] for r in rows),
        "all_appends_complete": all(r["append_complete"] for r in rows),
        "all_segments_clean": all(s == ["clean"] for r in rows
                                  for s in [r["segment_close_status"]]),
    }


def decision_b(data: dict, universes: dict) -> dict:
    """B — descriptive panel evidence. Does NOT select a panel (Amendment 3)."""
    # per market, per window
    per_window = {}
    for key in sorted(data):
        w = data[key]["window"]
        expo = w["session_duration_s"]
        mk = {}
        for tk, m in w["tape"]["markets"].items():
            ob = m["orderbook_frames"]
            mk[tk] = {
                "orderbook_frames": ob,
                "trade_frames": m["trade_frames"],
                "ticker_frames": m["ticker_frames"],
                "total_frames": m["total_frames"],
                "share_of_venue_traffic": m["share_of_venue_traffic"],
                # Amendment 4: normalise by THIS window's observed exposure.
                "ob_rate_per_s_observed": round(ob / expo, 4),
                "trade_rate_per_s_observed": round(m["trade_frames"] / expo, 4),
            }
        per_window[key] = mk

    # rank stability -- within day, market level, over that day's 3 slot pairs
    within = {}
    for day in DAYS:
        pairs = {}
        for i in range(len(SLOTS)):
            for j in range(i + 1, len(SLOTS)):
                a, b = per_window[(day, SLOTS[i])], per_window[(day, SLOTS[j])]
                common = sorted(set(a) & set(b))
                pairs[f"{SLOTS[i]}~{SLOTS[j]}"] = {
                    "n_markets": len(common),
                    "rho_orderbook": spearman(
                        [a[t]["ob_rate_per_s_observed"] for t in common],
                        [b[t]["ob_rate_per_s_observed"] for t in common]),
                    "rho_trade": spearman(
                        [a[t]["trade_rate_per_s_observed"] for t in common],
                        [b[t]["trade_rate_per_s_observed"] for t in common]),
                }
        within[day] = pairs

    # rank stability -- across days, SERIES level only (L23: identity does not persist)
    def series_totals(day):
        agg = {}
        for slot in SLOTS:
            expo = data[(day, slot)]["window"]["session_duration_s"]
            for tk, m in per_window[(day, slot)].items():
                s = agg.setdefault(series_of(tk), {"ob": 0.0, "tr": 0.0, "expo": 0.0})
                s["ob"] += m["orderbook_frames"]
                s["tr"] += m["trade_frames"]
            agg.setdefault("__expo__", {"ob": 0.0, "tr": 0.0, "expo": 0.0})["expo"] += expo
        expo_total = agg.pop("__expo__")["expo"]
        return {k: {"ob_rate": v["ob"] / expo_total, "tr_rate": v["tr"] / expo_total}
                for k, v in agg.items()}

    s1, s2 = series_totals(DAYS[0]), series_totals(DAYS[1])
    common_s = sorted(set(s1) & set(s2))
    across = {
        "level": "series (L23: market identity does not persist across days)",
        "n_series_common": len(common_s),
        "series": common_s,
        "rho_orderbook": spearman([s1[s]["ob_rate"] for s in common_s],
                                  [s2[s]["ob_rate"] for s in common_s]),
        "rho_trade": spearman([s1[s]["tr_rate"] for s in common_s],
                              [s2[s]["tr_rate"] for s in common_s]),
    }

    # persistence vs burstiness, and the s4 activity floor, pooled per day
    persistence = {}
    for day in DAYS:
        ctrl = universes[day]["positive_control"]
        rows = []
        allm = set()
        for slot in SLOTS:
            allm |= set(per_window[(day, slot)])
        for tk in sorted(allm):
            counts, obs = [], []
            for slot in SLOTS:
                m = per_window[(day, slot)].get(tk)
                counts.append(m["total_frames"] if m else 0)
                obs.append(m["orderbook_frames"] if m else 0)
            # Each subscribed market receives exactly one snapshot at subscribe
            # (orderbook_snapshot count == markets_subscribed in every window),
            # so deltas = orderbook_frames - 1 per window it appeared in.
            appeared = sum(1 for o in obs if o > 0)
            deltas = sum(o - 1 for o in obs if o > 0)
            rows.append({
                "ticker": tk,
                "series": series_of(tk),
                "is_positive_control": tk == ctrl,
                "windows_present": appeared,
                "median_total_frames": statistics.median(counts),
                "max_total_frames": max(counts),
                "burstiness_max_over_median": (
                    round(max(counts) / statistics.median(counts), 2)
                    if statistics.median(counts) > 0 else None),
                "pooled_orderbook_deltas": deltas,
                "pooled_snapshots": appeared,
                "clears_s4_activity_floor": (
                    deltas >= S4_MIN_DELTAS and appeared >= S4_MIN_SNAPSHOTS),
                "windows_clearing_floor_alone": sum(
                    1 for o in obs if (o - 1) >= S4_MIN_DELTAS),
            })
        persistence[day] = rows

    # s7 positive control MUST FAIL s4
    control = {}
    for day in DAYS:
        ctrl = universes[day]["positive_control"]
        row = next((r for r in persistence[day] if r["ticker"] == ctrl), None)
        control[day] = {
            "ticker": ctrl,
            "present_in_any_window": row is not None and row["windows_present"] > 0,
            "pooled_orderbook_deltas": row["pooled_orderbook_deltas"] if row else 0,
            "clears_s4_activity_floor": bool(row and row["clears_s4_activity_floor"]),
            "VERDICT": ("PASS - control correctly FAILS s4"
                        if not (row and row["clears_s4_activity_floor"])
                        else "RUN IS VOID - control PASSED s4"),
        }
    return {"per_window": {f"{d} {s}": v for (d, s), v in per_window.items()},
            "rank_stability_within_day": within,
            "rank_stability_across_days": across,
            "persistence": persistence,
            "positive_control": control}


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv[1:])
    base = Path(a.base)
    data = load(base)
    universes = {d: json.loads((base / f"day={d}" / "universe.json").read_text())
                 for d in DAYS}
    result = {
        "milestone": "PROD-ACTIVITY-PROFILE-001",
        "phase": "analysis",
        "governed_by": ["Amendment 3 (analysis stage)",
                        "Amendment 4 (capped_events is right-censored)"],
        "windows_analysed": [f"{d} {s}" for d in DAYS for s in SLOTS],
        "A_capacity": decision_a(data),
        "B_panel_evidence": decision_b(data, universes),
    }
    Path(a.out).write_text(json.dumps(result, indent=2, sort_keys=True, default=str))
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv))
