"""End-of-session verdict for one MARKET-MICROSTRUCTURE-EDGE-001 tranche session.

Four layers, and nothing else. This tool is deliberately incapable of saying
anything about M0/M1 performance: it never fits a model, never correlates a
feature with a label, and never ranks a session as promising. It reports
capture health, sampling conformance, dataset structure and coverage.

Blind-capture discipline (tranche ledger): permitted are capture health,
sequence/archive integrity, safety peak, row/schema validity, mechanical power
accumulation and coverage. Forbidden are returns by feature, M0/M1 losses,
coefficients, feature importance, markout relationships and any judgement about
which sessions look interesting.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.microstructure import features as F           # noqa: E402
from app.microstructure import panel as P              # noqa: E402
from app.microstructure.rows import (                  # noqa: E402
    LABEL_SCHEMA_VERSION, PanelSchedule, ROW_SCHEMA_VERSION, build_rows)
from app.microstructure.panel import DatasetRole, MarketMeta  # noqa: E402

REST = "https://api.elections.kalshi.com/trade-api/v2"

#: Why a subscribed market produced no rows. These are different facts and the
#: ledger must not conflate them.
NO_ROWS_CLOSED = "market_naturally_closed_or_resolved"
NO_ROWS_QUIET = "market_stayed_open_but_failed_activity_eligibility"
NO_ROWS_OUTRANKED = "market_eligible_but_never_reached_the_top_K"
NO_ROWS_UNKNOWN = "status_unavailable"


def market_status(ticker: str) -> str | None:
    """Read-only GET of one market's lifecycle status. Not an alpha quantity."""
    try:
        req = urllib.request.Request(f"{REST}/markets/{ticker}",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return (json.load(r).get("market") or {}).get("status")
    except Exception:
        return None


def bin_of(tte: float) -> str:
    return P.tte_bin(tte)


def interval_wholly_within(tte_start: float, target_bin: str) -> bool:
    """Does the 300 s panel interval starting here lie WHOLLY inside the bin?

    TTE decreases monotonically through the interval, so both endpoints must
    land in the same bin. This is the frozen coverage rule -- a session that
    merely grazes a bin for one second does not count.
    """
    return (bin_of(tte_start) == target_bin
            and bin_of(tte_start - P.DECISION_TICK_S) == target_bin)


def verdict(session_path: Path, events_path: Path, target_bin: str,
            *, poll_status: bool = True) -> dict:
    S = json.loads(session_path.read_text())
    ev = json.loads(events_path.read_text())
    markets = {t: MarketMeta(
        t, ev[t]["series"],
        datetime.fromisoformat(ev[t]["occurrence_datetime"].replace("Z", "+00:00")))
        for t in ev}
    sr = S.get("session_result", {})

    # ---- layer 1: capture validity ------------------------------------
    L1 = {
        "terminal_status": sr.get("status"),
        "reached_expected_terminal_condition": sr.get("status") == "capped_time",
        "events_received": sr.get("events_received"),
        "events_archived": sr.get("events_archived"),
        "conserved": sr.get("events_received") == sr.get("events_archived"),
        "frames_malformed": sr.get("frames_malformed"),
        "events_rejected": sr.get("events_rejected"),
        "rotation_failures": sr.get("rotation_failures"),
        "sequence_faults": sr.get("sequence_faults"),
        "reconnects": sr.get("reconnects"),
        "segments_committed": sr.get("segments_committed"),
        "peak_1s_sliding": S["safety"]["observed_peak_fps"],
        "safety_status": S["safety"]["status"],
        "under_hard_stop": not S["safety"]["breached"],
    }
    L1["PASS"] = bool(
        L1["reached_expected_terminal_condition"] and L1["conserved"]
        and not L1["frames_malformed"] and not L1["events_rejected"]
        and not L1["rotation_failures"] and not L1["sequence_faults"]
        and L1["under_hard_stop"])

    # ---- layer 2: sampling validity -----------------------------------
    ticks = S["decision_ticks"]
    times = [datetime.fromisoformat(d["tick_t"]) for d in ticks]
    gaps = {round((b - a).total_seconds(), 3) for a, b in zip(times, times[1:])}
    open_t = datetime.fromisoformat(S["session_open"])
    allowed_reasons = {
        None, P.NOT_SELECTED_WARMUP, P.NOT_SELECTED_LOW_ACTIVITY,
        P.NOT_SELECTED_BOOK_UNPUBLISHABLE, P.NOT_SELECTED_SEQUENCE_FAULT,
        P.NOT_SELECTED_SESSION_TOO_SHORT, P.NOT_SELECTED_RANK}
    seen_reasons = {a["reason_if_not_selected"] for d in ticks for a in d["audit"]}
    L2 = {
        "first_tick_at_open_plus_300s":
            bool(times) and times[0] == open_t + timedelta(seconds=P.WARMUP_S),
        "no_warmup_tick_emitted_a_panel":
            all(not d["panel"] for d in ticks if d["is_warmup"]),
        "tick_gaps_seconds": sorted(gaps),
        "cadence_exactly_300s": gaps <= {float(P.DECISION_TICK_S)},
        "max_panel_size": max((len(d["panel"]) for d in ticks), default=0),
        "k_respected": all(len(d["panel"]) <= S["panel_k"] for d in ticks),
        "reason_vocabulary_closed": seen_reasons <= allowed_reasons,
        "unexpected_reasons": sorted(str(r) for r in seen_reasons - allowed_reasons),
        "eligibility_inputs": ["lagged sequenced orderbook activity",
                               "current-generation book state",
                               "sequence-fault state", "session remaining"],
        "ticker_or_volume_in_eligibility": False,   # proven by AST guard in tests
    }
    # A PASS on zero ticks is VACUOUS -- every clause is trivially true when
    # nothing happened, which is the doctrine-7 shape: a green light that means
    # "we measured nothing". S04 exposed this by producing 0 ticks and passing.
    L2["ticks"] = len(ticks)
    if not ticks:
        L2["PASS"] = False
        L2["VACUOUS"] = True
        L2["note"] = ("no decision tick occurred, so no sampling property was "
                      "exercised; this is NOT a pass")
    else:
        L2["VACUOUS"] = False
        L2["PASS"] = bool(L2["first_tick_at_open_plus_300s"]
                          and L2["no_warmup_tick_emitted_a_panel"]
                          and L2["cadence_exactly_300s"] and L2["k_respected"]
                          and L2["reason_vocabulary_closed"])

    # ---- layer 3: dataset validity ------------------------------------
    sched = PanelSchedule(ticks=[
        (datetime.fromisoformat(d["tick_t"]).timestamp() * 1000,
         frozenset(d["panel"]), f"tick{i}") for i, d in enumerate(ticks)])
    built = build_rows(env_dir=Path(S["archive_root"]) / "env=production",
                       panel_schedule=sched, markets=markets,
                       session_id=S["session_id"],
                       capture_commit=S["capture_commit"],
                       # read from the session, never assumed -- pointing
                       # this at a VALIDATION tape must not relabel it
                       dataset_role=S["dataset_role"],
                       session_status=S["safety"]["status"],
                       preregistration_version=S["preregistration_version"])
    rows, rep = built["rows"], built["report"]
    always_missing = sorted(
        [k for k, v in rep["m0_completeness"].items() if v == 0.0]
        + [k for k, v in rep["m1_completeness"].items() if v == 0.0])
    L3 = {
        "row_schema_version": rep["row_schema_version"],
        "label_schema_version": rep["label_schema_version"],
        "schema_is_v2": (rep["row_schema_version"] == ROW_SCHEMA_VERSION
                         and rep["label_schema_version"] == LABEL_SCHEMA_VERSION),
        "m0_columns": len(F.M0_FEATURES), "m1_flow_columns": len(F.M1_FLOW_FEATURES),
        "m0_min_completeness": min(rep["m0_completeness"].values(), default=None),
        "m1_min_completeness": min(rep["m1_completeness"].values(), default=None),
        "always_missing_columns": always_missing,
        "label_coverage": rep["label_coverage"],
        "dataset_role": S["dataset_role"],
        "role_is_confirmation": S["dataset_role"] == DatasetRole.CONFIRMATION,
        "rows_emitted": rep["rows_emitted"],
        "dispatch_errors": rep["dispatch_errors"],
    }
    # Same trap: `always_missing_columns` is empty when there are no rows to
    # check, and completeness is None. Passing on that would certify an empty
    # dataset as a good one.
    if not rows:
        L3["PASS"] = False
        L3["VACUOUS"] = True
        L3["note"] = ("no research row was emitted, so no dataset property "
                      "was exercised; this is NOT a pass")
    else:
        L3["VACUOUS"] = False
        L3["PASS"] = bool(L3["schema_is_v2"] and not always_missing
                          and not L3["dispatch_errors"])

    # ---- layer 4: coverage outcome ------------------------------------
    covering = []
    for i, d in enumerate(ticks):
        t = datetime.fromisoformat(d["tick_t"])
        for tk in d["panel"]:
            tte = (markets[tk].occurrence_datetime - t).total_seconds()
            if interval_wholly_within(tte, target_bin):
                covering.append((f"tick{i}", tk))
    selected = {a["market"] for d in ticks for a in d["audit"] if a["selected"]}
    ever_eligible = {a["market"] for d in ticks for a in d["audit"] if a["eligible"]}
    subscribed = set(S["subscribed"])
    no_rows = subscribed - selected

    reasons = {}
    for tk in sorted(no_rows):
        if tk in ever_eligible:
            reasons[tk] = NO_ROWS_OUTRANKED
            continue
        if not poll_status:
            reasons[tk] = NO_ROWS_UNKNOWN
            continue
        st = market_status(tk)
        reasons[tk] = (NO_ROWS_UNKNOWN if st is None
                       else NO_ROWS_CLOSED if st != "open" else NO_ROWS_QUIET)

    blocks = rep["rows_emitted"] // P.DECISION_TICK_S
    L4 = {
        "target_bin": target_bin,
        "covering_intervals": len(covering),
        "counts_toward_target_bin": len(covering) > 0,
        "markets_ever_eligible": len(ever_eligible),
        "markets_ever_selected": len(selected),
        "subscribed": len(subscribed),
        "market_blocks_accumulated": blocks,
        "market_session_clusters": rep["unique_market_session_clusters"],
        "tte_bins_touched": rep["tte_bins_represented"],
        "cleared_activity_floor": sorted(ever_eligible),
        "no_row_reasons": reasons,
        "no_row_reason_counts": {
            r: sum(1 for v in reasons.values() if v == r)
            for r in sorted(set(reasons.values()))},
    }

    return {"milestone": "MARKET-MICROSTRUCTURE-EDGE-001",
            "phase": "tranche_session_verdict", "label": S["label"],
            "session_id": S["session_id"], "target_bin": target_bin,
            "L1_capture_validity": L1, "L2_sampling_validity": L2,
            "L3_dataset_validity": L3, "L4_coverage_outcome": L4,
            "OPERATIONALLY_CLEAN": bool(L1["PASS"] and L2["PASS"] and L3["PASS"]),
            "CAPTURE_HEALTHY_BUT_EMPTY": bool(
                L1["PASS"] and (L2.get("VACUOUS") or L3.get("VACUOUS"))),
            "counts_toward_target_bin": L4["counts_toward_target_bin"]}


def main(argv) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--events", required=True)
    ap.add_argument("--target-bin", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-status-poll", action="store_true")
    a = ap.parse_args(argv[1:])
    v = verdict(Path(a.session), Path(a.events), a.target_bin,
                poll_status=not a.no_status_poll)
    Path(a.out).write_text(json.dumps(v, indent=2, default=str))
    print(json.dumps({k: (val if not isinstance(val, dict) else val.get("PASS", val))
                      for k, val in v.items()}, indent=2, default=str)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
