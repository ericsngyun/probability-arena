"""CRYPTO-HORIZON-CANDIDATE-READINESS-001 — local, read-only operational
readiness evaluator for the manually authorized shared-pass horizon canary.

Detects the rare moment when the ALREADY-PERSISTED local database holds an exact
two-token candidate set that an operator could — by hand, under explicit
authorization — turn into a shared-pass horizon cohort: two complete-state tokens
whose 15m planner windows overlap, that can be simultaneously ``due_now``, whose
45-second activation grace fits, and with enough margin left to run the
explicit-selection dry-run, create the cohort, enter shared ``due_now``, arm, and
post-install verify.

This is operational-canary readiness measurement, manual review required. It never
calls a provider, discovers, creates/arms a cohort, installs a unit, triggers an
observation, ranks by market performance, or emits EV/recommendations. It COMPOSES
the deployed feasibility calculations (``_completeness_reason``, ``fifteen_window``,
``pair_feasibility``, ``safe_arm_deadline``, ``ACTIVATION_GRACE``) so the completeness
and shared-window rules can never drift into a second implementation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import CryptoWatcherRun
from app.services.crypto_horizon import _completeness_reason, _valid_token_id
from app.services.crypto_horizon_feasibility import (
    ACTIVATION_GRACE,
    _Anchor,
    _load_anchors,
    fifteen_window,
    pair_feasibility,
    safe_arm_deadline,
)
from app.services.crypto_horizon_orchestrator import HOST_HOME
from app.services.crypto_horizon_schedule import format_los_angeles, format_utc
from app.services.crypto_tape import HORIZONS, _aware, _now

READINESS_NOTE = (
    "Operational canary readiness measurement — manual review required. Local "
    "read-only: zero provider calls, zero writes from the report path, no "
    "discovery, cohort, arming, observation, EV, or recommendation."
)

# --- readiness states (distinct semantics) --------------------------------------
NO_COMPLETE_CANDIDATES = "no_complete_candidates"
NO_OVERLAPPING_PAIR = "no_overlapping_pair"
PAIR_DETECTED_NOT_DUE = "pair_detected_not_due"
PAIR_READY_FOR_MANUAL_PREPARATION = "pair_ready_for_manual_preparation"
SHARED_DUE_NOW_READY = "shared_due_now_ready"
INSUFFICIENT_ARM_SLACK = "insufficient_arm_slack"
EXPIRED = "expired"

# readiness priority: higher == more operationally ready. Used to pick the report's
# overall state and the highest-priority pair to detail.
_STATE_PRIORITY = {
    SHARED_DUE_NOW_READY: 6,
    PAIR_READY_FOR_MANUAL_PREPARATION: 5,
    PAIR_DETECTED_NOT_DUE: 4,
    INSUFFICIENT_ARM_SLACK: 3,
    EXPIRED: 2,
    NO_OVERLAPPING_PAIR: 1,
    NO_COMPLETE_CANDIDATES: 0,
}

# Minimum operator margin, BEYOND the 45s activation grace, to complete the five
# manual canary steps under human-in-the-loop review: explicit-selection dry-run,
# atomic cohort creation, orchestrator dry-run, confirmed arming, and post-install
# verification. The canary procedures encode no such constant, so this is a named
# internal MEASUREMENT constant (not an environment flag). Override per-evaluation
# with --minimum-arm-margin-seconds; never relaxes completeness.
OPERATOR_PREP_MARGIN_SECONDS = 180.0

# beyond this anchor separation two 15m windows cannot intersect
DEFAULT_NEIGHBORHOOD_MINUTES = 15

PAIR_ORDERING = (
    "deterministic, operational only: (1) greatest remaining safe shared-window "
    "slack, then (2) earliest shared-window close, then (3) canonical token id "
    "ascending as a stable tie-breaker. Never price/volume/momentum/EV."
)


def classify_pair(
    shared_open: datetime | None, shared_close: datetime | None,
    now: datetime, *, grace_fits: bool, margin_seconds: float,
) -> str:
    """Map one overlapping pair's shared 15m window + now to a readiness state.
    ``grace_fits`` is the deployed ``activation_grace_fits_shared_window``."""
    if shared_open is None or shared_close is None:
        return NO_OVERLAPPING_PAIR
    if not grace_fits:
        return INSUFFICIENT_ARM_SLACK
    deadline = safe_arm_deadline(shared_close, margin_seconds)  # close - grace - margin
    if deadline < shared_open:
        # window too narrow for grace + operator margin at any instant
        return INSUFFICIENT_ARM_SLACK
    if now > shared_close:
        return EXPIRED
    if now > deadline:
        return INSUFFICIENT_ARM_SLACK
    if now < shared_open - timedelta(seconds=margin_seconds):
        return PAIR_DETECTED_NOT_DUE
    if now < shared_open:
        return PAIR_READY_FOR_MANUAL_PREPARATION
    return SHARED_DUE_NOW_READY


def _all_horizons_feasible(anchor: datetime, now: datetime) -> bool:
    for label, minutes in HORIZONS:
        target = anchor + timedelta(minutes=minutes)
        end = target + timedelta(minutes=minutes * 0.5)
        if end < now:
            return False
    return True


def _pair_record(a: _Anchor, b: _Anchor, now: datetime, margin: float) -> dict:
    pf = pair_feasibility(a.anchor, a.persist, b.anchor, b.persist, now, margin_seconds=margin)
    # a shared PASS needs every horizon's shared window nonempty AND the 45s grace
    # to fit — that is exactly `shared_pass_eligible`; gate arm-ability on it.
    state = classify_pair(pf["shared_15m_open"], pf["shared_15m_close"], now,
                          grace_fits=pf["shared_pass_eligible"], margin_seconds=margin)
    deadline = (safe_arm_deadline(pf["shared_15m_close"], margin)
                if pf["shared_15m_close"] else None)
    slack = round((deadline - now).total_seconds(), 1) if deadline else None
    ta, tb = sorted((a.token, b.token))
    return {
        "state": state,
        "token_a": ta, "token_b": tb,
        "symbol_a": a.symbol if a.token == ta else b.symbol,
        "symbol_b": b.symbol if b.token == tb else a.symbol,
        "shared_15m_open": pf["shared_15m_open"],
        "shared_15m_close": pf["shared_15m_close"],
        "grace_fits": pf["grace_fits"],
        "shared_pass_eligible": pf["shared_pass_eligible"],
        "latest_safe_arm": deadline,
        "remaining_safe_slack_seconds": slack,
        "_a": a, "_b": b, "_pf": pf,
    }


def _token_detail(anc: _Anchor, now: datetime) -> dict:
    b = anc.birth
    open_, close = fifteen_window(anc.anchor)
    target = anc.anchor + timedelta(minutes=15)
    if now < open_:
        pstate = "not_due"
    elif now <= close:
        pstate = "due_now"
    else:
        pstate = "overdue"
    return {
        "token": anc.token, "symbol": anc.symbol,
        "first_evidence_at": format_utc(anc.anchor),
        "persisted_at": format_utc(anc.persist),
        "launch_source": anc.source, "pair_venue": anc.dex,
        "initial_pair": b.first_pair_address,
        "initial_price_usd": b.initial_price_usd,
        "initial_liquidity_usd": b.initial_liquidity_usd,
        "completeness": _completeness_reason(b, 0.0) or "complete",
        "nominal_15m_target": format_utc(target),
        "window_15m_start": format_utc(open_),
        "window_15m_close": format_utc(close),
        "planner_state": pstate,
    }


def evaluate_readiness(
    session: Session, *, now: datetime | None = None, chain: str = "solana",
    min_arm_margin_seconds: float = OPERATOR_PREP_MARGIN_SECONDS,
    require_complete: bool = True, neighborhood_minutes: int = DEFAULT_NEIGHBORHOOD_MINUTES,
    limit: int = 5, marketops_cycle_id: int | None = None,
) -> dict:
    """Evaluate current shared-pass readiness from persisted local data only.
    Read-only; zero external calls. ``require_complete`` cannot be relaxed for the
    live verdict — it is always applied to actionable states."""
    now = _aware(now) or _now()
    margin = max(0.0, float(min_arm_margin_seconds))
    anchors = _load_anchors(session, chain)

    rejected: list[dict] = []
    valid: list[_Anchor] = []
    for a in anchors:
        if not _valid_token_id(a.token):
            rejected.append({"token": a.token, "reason": "malformed_identifier"})
            continue
        if a.anchor is None:
            rejected.append({"token": a.token, "reason": "missing_first_evidence"})
            continue
        reason = _completeness_reason(a.birth, 0.0)  # deployed rule; never relaxed
        if reason is not None:
            # incomplete candidates are simply not eligible; record concise reason
            rejected.append({"token": a.token, "reason": reason})
            continue
        valid.append(a)

    complete = valid
    feasible_15m = [a for a in complete if fifteen_window(a.anchor)[1] >= now]
    all_feasible = [a for a in complete if _all_horizons_feasible(a.anchor, now)]

    # candidate pairs within the neighborhood over ALL complete tokens (time-based
    # classification distinguishes ready / not-due / insufficient / expired — a pair
    # is not filtered out merely because its window closed; that is the `expired`
    # signal). Pairing over complete-only never relaxes completeness.
    ordered = sorted(complete, key=lambda a: a.anchor)
    horizon = timedelta(minutes=neighborhood_minutes)
    seen_pairs: set = set()
    pairs: list[dict] = []
    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if ordered[j].anchor - ordered[i].anchor > horizon:
                break
            if ordered[i].token == ordered[j].token:  # identity conflict guard
                rejected.append({"token": ordered[i].token, "reason": "conflicting_identity"})
                continue
            key = tuple(sorted((ordered[i].token, ordered[j].token)))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            rec = _pair_record(ordered[i], ordered[j], now, margin)
            if rec["shared_15m_open"] is not None:
                pairs.append(rec)

    overlapping = pairs
    usable = [p for p in overlapping
              if p["state"] in (SHARED_DUE_NOW_READY, PAIR_READY_FOR_MANUAL_PREPARATION)]

    # overall state
    if len(complete) < 2:
        overall = NO_COMPLETE_CANDIDATES
    elif not overlapping:
        overall = NO_OVERLAPPING_PAIR
    else:
        overall = max((p["state"] for p in overlapping), key=lambda s: _STATE_PRIORITY[s])

    # deterministic ordering: greatest slack, then earliest close, then token ids
    def _key(p):
        slack = p["remaining_safe_slack_seconds"]
        slack = slack if slack is not None else -1e18
        return (-slack, p["shared_15m_close"] or datetime.max.replace(tzinfo=timezone.utc),
                p["token_a"], p["token_b"])

    # detail a pair only for actionable / watch states (not expired/insufficient)
    _DETAILED = (SHARED_DUE_NOW_READY, PAIR_READY_FOR_MANUAL_PREPARATION, PAIR_DETECTED_NOT_DUE)
    best_state_pairs = [p for p in overlapping if p["state"] == overall]
    best_state_pairs.sort(key=_key)
    top = best_state_pairs[0] if (best_state_pairs and overall in _DETAILED) else None

    fe = [a.anchor for a in anchors if a.anchor]
    persists = [a.persist for a in anchors if a.persist]
    latest_run = session.execute(select(func.max(CryptoWatcherRun.id))).scalar()

    top_detail = None
    if top is not None:
        pf = top["_pf"]
        top_detail = {
            "token_a": top["token_a"], "token_b": top["token_b"],
            "symbol_a": top["symbol_a"], "symbol_b": top["symbol_b"],
            "state": top["state"],
            "shared_window_open_utc": format_utc(top["shared_15m_open"]),
            "shared_window_close_utc": format_utc(top["shared_15m_close"]),
            "activation_grace_adjusted_earliest_exec_utc": format_utc(
                (top["shared_15m_open"] or now) + ACTIVATION_GRACE),
            "earliest_safe_preparation_utc": format_utc(
                (top["shared_15m_open"] - timedelta(seconds=margin))
                if top["shared_15m_open"] else None),
            "latest_safe_cohort_creation_utc": format_utc(top["latest_safe_arm"]),
            "latest_safe_arming_utc": format_utc(top["latest_safe_arm"]),
            "remaining_safe_slack_seconds": top["remaining_safe_slack_seconds"],
            "shared_pass_eligible": top["shared_pass_eligible"],
            "grace_fits": top["grace_fits"],
            "not_ready_reason": (None if top["state"] in (
                SHARED_DUE_NOW_READY, PAIR_READY_FOR_MANUAL_PREPARATION) else top["state"]),
            "token_a_detail": _token_detail(top["_a"] if top["_a"].token == top["token_a"]
                                            else top["_b"], now),
            "token_b_detail": _token_detail(top["_b"] if top["_b"].token == top["token_b"]
                                            else top["_a"], now),
        }

    return {
        "state": overall,
        "note": READINESS_NOTE,
        "external_calls": 0,
        "persisted": False,
        "writes": 0,
        "manual_action_required": True,
        "automatic_cohort_creation": False,
        "automatic_arming": False,
        "evaluated_at_utc": format_utc(now),
        "evaluated_at_los_angeles": format_los_angeles(now),
        "database_coverage_utc": format_utc(max(fe)) if fe else None,
        "database_persist_max_utc": format_utc(max(persists)) if persists else None,
        "latest_discovery_run_id": latest_run,
        "marketops_cycle_id": marketops_cycle_id,
        "activation_grace_seconds": ACTIVATION_GRACE.total_seconds(),
        "minimum_arm_margin_seconds": margin,
        "require_complete": require_complete,
        "neighborhood_minutes": neighborhood_minutes,
        "pair_ordering": PAIR_ORDERING,
        "counts": {
            "candidates": len(anchors),
            "complete_candidates": len(complete),
            "feasible_15m_candidates": len(feasible_15m),
            "all_horizons_feasible_candidates": len(all_feasible),
            "overlapping_pairs": len(overlapping),
            "usable_pairs": len(usable),
            "rejected": len(rejected),
        },
        "top_pair": top_detail,
        "rejected": rejected[:limit],
        "pairs": [{k: (format_utc(v) if isinstance(v, datetime) else v)
                   for k, v in p.items() if not k.startswith("_")}
                  for p in best_state_pairs[:limit]],
    }


def proposed_commands(readiness: dict) -> list[str]:
    """Operator-review PROPOSALS only — never executed, never auto-confirmed.
    Emits the dry-run selection command for the top usable pair; deliberately
    does NOT emit a --confirm creation or an arming command."""
    top = readiness.get("top_pair")
    if not top or readiness["state"] not in (
        SHARED_DUE_NOW_READY, PAIR_READY_FOR_MANUAL_PREPARATION):
        return []
    return [
        "# OPERATOR-REVIEW PROPOSAL ONLY — not executed, no authorization implied",
        "python -m app.cli crypto-horizon-cohort-create \\",
        f"  --token {top['token_a']} \\",
        f"  --token {top['token_b']} \\",
        "  --require-complete \\",
        "  --require-shared-horizon-windows \\",
        "  --dry-run",
    ]


# --- append-only readiness audit (written ONLY by the gated MarketOps hook) ------

READINESS_AUDIT_ROOT = HOST_HOME / "crypto-horizon-readiness"
READINESS_AUDIT_FILE = READINESS_AUDIT_ROOT / "readiness.jsonl"


def readiness_audit_record(readiness: dict, *, run_id: int | None) -> dict:
    """The minimal, secret-free, append-only record for one evaluation."""
    top = readiness.get("top_pair") or {}
    return {
        "run_id": run_id,
        "marketops_cycle_id": readiness.get("marketops_cycle_id"),
        "evaluated_at_utc": readiness.get("evaluated_at_utc"),
        "state": readiness.get("state"),
        "external_calls": 0,
        "candidate_token_a": top.get("token_a"),
        "candidate_token_b": top.get("token_b"),
        "shared_window_open_utc": top.get("shared_window_open_utc"),
        "shared_window_close_utc": top.get("shared_window_close_utc"),
        "remaining_safe_slack_seconds": top.get("remaining_safe_slack_seconds"),
        "overlapping_pairs": readiness.get("counts", {}).get("overlapping_pairs"),
        "usable_pairs": readiness.get("counts", {}).get("usable_pairs"),
        "rejection_reason": (readiness.get("state")
                             if readiness.get("top_pair") is None else None),
    }


def append_readiness_record(record: dict, path: Path = READINESS_AUDIT_FILE) -> None:
    """Append one JSONL line. The ONLY write in this module; used solely by the
    gated MarketOps measurement hook (never by the report CLI)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def load_readiness_records(path: Path = READINESS_AUDIT_FILE) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# --- history aggregation over accumulated readiness records ---------------------

_READY_STATES = (SHARED_DUE_NOW_READY, PAIR_READY_FOR_MANUAL_PREPARATION)


def build_readiness_history_report(records: list[dict], *, limit: int = 20) -> dict:
    """Bounded aggregate over accumulated readiness evaluations. Groups consecutive
    same-pair ready cycles into moments; reports coverage, state counts, distinct
    moments/pairs, per-moment duration + slack, and a clearly-labeled estimate of
    moments missed between cycles. No market-performance output."""
    def _t(rec):
        v = rec.get("evaluated_at_utc")
        if not v:
            return None
        return datetime.fromisoformat(v.replace("Z", "+00:00"))

    recs = sorted([r for r in records if _t(r) is not None], key=_t)
    n = len(recs)
    by_state: dict[str, int] = {}
    for r in recs:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1

    # inter-cycle gaps (cadence)
    gaps = [ (_t(recs[i]) - _t(recs[i - 1])).total_seconds() for i in range(1, n) ]
    gaps_sorted = sorted(gaps)
    median_gap = gaps_sorted[len(gaps_sorted) // 2] if gaps_sorted else None

    # moments = maximal runs of consecutive records that are ready for the SAME pair
    moments = []
    cur = None
    for r in recs:
        ready = r["state"] in _READY_STATES
        pair = (r.get("candidate_token_a"), r.get("candidate_token_b")) if ready else None
        if ready and cur is not None and cur["pair"] == pair:
            cur["end"] = _t(r)
            cur["cycles"] += 1
            if r.get("remaining_safe_slack_seconds") is not None:
                cur["slacks"].append(r["remaining_safe_slack_seconds"])
        else:
            if cur is not None:
                moments.append(cur)
            cur = ({"pair": pair, "start": _t(r), "end": _t(r), "cycles": 1,
                    "slacks": ([r["remaining_safe_slack_seconds"]]
                               if r.get("remaining_safe_slack_seconds") is not None else [])}
                   if ready else None)
    if cur is not None:
        moments.append(cur)

    distinct_pairs = {m["pair"] for m in moments}
    days = {}
    all_slacks = []
    moment_out = []
    for m in moments:
        duration = (m["end"] - m["start"]).total_seconds()
        day = m["start"].date().isoformat()
        days[day] = days.get(day, 0) + 1
        all_slacks.extend(m["slacks"])
        moment_out.append({
            "pair": list(m["pair"]) if m["pair"] else None,
            "start_utc": m["start"].isoformat().replace("+00:00", "Z"),
            "end_utc": m["end"].isoformat().replace("+00:00", "Z"),
            "duration_seconds": round(duration, 1),
            "cycles": m["cycles"],
            "max_slack_seconds": max(m["slacks"]) if m["slacks"] else None,
        })
    all_slacks.sort()

    # missed-between-cycle ESTIMATE: single-cycle moments (cycles==1) plausibly
    # spanned less than one cadence gap; a moment shorter than the median gap could
    # have fallen entirely between two cycles on a different phase. Clearly an
    # estimate, derived only from observed cadence, never asserted as ground truth.
    single_cycle_moments = sum(1 for m in moments if m["cycles"] == 1)

    return {
        "note": READINESS_NOTE,
        "external_calls": 0,
        "evaluations": n,
        "history_coverage_utc": {
            "first": recs[0]["evaluated_at_utc"] if recs else None,
            "last": recs[-1]["evaluated_at_utc"] if recs else None,
        },
        "median_cycle_gap_seconds": median_gap,
        "count_by_state": dict(sorted(by_state.items(), key=lambda x: -x[1])),
        "distinct_ready_moments": len(moments),
        "distinct_candidate_pairs": len([p for p in distinct_pairs if p]),
        "ready_moments_by_day": dict(sorted(days.items())),
        "max_arm_slack_seconds": all_slacks[-1] if all_slacks else None,
        "median_arm_slack_seconds": all_slacks[len(all_slacks) // 2] if all_slacks else None,
        "estimated_single_cycle_moments": single_cycle_moments,
        "missed_between_cycle_estimate_note": (
            "ESTIMATE ONLY: single-cycle ready moments may indicate windows short "
            "relative to the ~{}s median MarketOps cadence; not ground truth.".format(
                int(median_gap) if median_gap else "n/a")),
        "moments": moment_out[:limit],
    }
