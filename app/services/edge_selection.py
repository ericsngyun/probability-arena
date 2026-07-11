"""EDGE-SELECTION-001 — pre-registered, read-only validation protocol for
candidate row-selection policies.

EDGE-FILTER-001 discovered follows-move+quality cohorts with positive shadow
closure, and TRIGGER-TIMING-001 confirmed the mechanism is row SELECTION, not
measurement timing. But those cohorts were found by searching 18 policies on
the same windows that now score them — a textbook overfitting risk, made vivid
when `require_gap_follows_move_totals_only` cleared the shadow MVP bar on one
48h window (2026-07-09 ~17:45 UTC) and fell back below it hours later.

This module implements the PRE-REGISTRATION protocol
(docs/EDGE_SELECTION_PREREG_2026_07_09.md): a frozen list of candidate
policies (plus a baseline and a negative control), locked BEFORE future
windows, evaluated against fixed success/failure gates on explicitly labelled
discovery vs validation windows. Discovery-window numbers can never validate a
candidate — they selected it. Only out-of-sample windows can.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): READ-ONLY VALIDATION
PROTOCOL. No live edge-precheck gate, threshold, promotion, forecaster,
MarketOps/EDGE-AUTO behavior, or flag changes — this re-slices rows that
already exist under policies that are already defined. `validated_shadow` is a
measurement-protocol status, never an authorization: MVP-005B remains blocked
unless a human explicitly accepts it, regardless of any status printed here.
Nothing is persisted; no external call is made.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.services.edge_filter_shadow import (
    FINAL_HORIZON,
    MAX_GAME_SHARE,
    MAX_TICKER_SHARE,
    MIN_READABLE_FINAL_N,
    POLICIES,
    summarize_policy,
)
from app.services.edge_followthrough import (
    EdgeFollowthroughDiagnosticService,
    _aware,
)

logger = logging.getLogger(__name__)

SELECTION_NOTE = (
    "Pre-registered read-only validation protocol. Policies were frozen in "
    "docs/EDGE_SELECTION_PREREG_2026_07_09.md BEFORE the windows that can "
    "validate them; discovery-window numbers selected these policies and can "
    "never validate them. Every number is measured market movement over rows "
    "that already exist — nothing is filtered live, nothing changes live. "
    "validated_shadow is a protocol status, never an authorization; not PnL, "
    "not EV, never advice. No sizing, orders, wallets, keys, swaps, signing, "
    "or execution."
)

MVP_005B_NOTE = (
    "MVP-005B remains blocked unless explicit human acceptance — regardless of "
    "any status in this report, including validated_shadow."
)

OVERFITTING_NOTE = (
    "Overfitting risk is the reason this protocol exists: these candidates were "
    "the best of 18 searched policies on the discovery windows, so their "
    "discovery numbers are upward-biased by selection. Evidence of that bias is "
    "already on record: require_gap_follows_move_totals_only cleared the shadow "
    "MVP bar on one 48h window (2026-07-09 ~17:45 UTC) and regressed below it "
    "within hours (24h toward 0.389 the same evening). Only windows that start "
    "after the pre-registration lock can validate anything."
)

PREREG_DOC = "docs/EDGE_SELECTION_PREREG_2026_07_09.md"

# The pre-registration lock instant. Rows created at or before this instant are
# DISCOVERY (in-sample) — they informed policy selection and cannot validate.
PREREG_LOCKED_AT = datetime(2026, 7, 9, 19, 0, 0, tzinfo=timezone.utc)

ROLE_BASELINE = "baseline"
ROLE_CANDIDATE = "candidate"
ROLE_NEGATIVE_CONTROL = "negative_control"

# The FROZEN registry: (policy_name in edge_filter_shadow.POLICIES, role,
# pre-registration alias). No policy may be added, removed, or reweighted
# without a NEW pre-registration document and lock — tests enforce the freeze.
PREREGISTERED: tuple[tuple[str, str, str], ...] = (
    ("baseline_all_watchlist", ROLE_BASELINE, "baseline_all_watchlist"),
    ("require_gap_follows_move_totals_only", ROLE_CANDIDATE, "require_gap_follows_move_totals_only"),
    ("require_gap_follows_move_exclude_spreads", ROLE_CANDIDATE, "require_gap_follows_move_exclude_spreads"),
    ("gap_follows_move_and_high_liquidity", ROLE_CANDIDATE, "gap_follows_move_and_high_liquidity"),
    ("gap_follows_move_and_tight_spread", ROLE_CANDIDATE, "gap_follows_move_and_tight_spread"),
    ("total_only", ROLE_CANDIDATE, "totals_only"),
    ("exclude_spread_markets", ROLE_CANDIDATE, "exclude_spreads"),
    ("spread_only", ROLE_NEGATIVE_CONTROL, "spread_only"),
)

# --- success gates (pre-registered; see the prereg document) ---------------------
VALIDATED_MIN_N = 75            # hard minimum final_n on a validation window
VALIDATED_PREFERRED_N = 150     # preferred final_n
VALIDATED_TOWARD_60 = 0.55      # 60m moved-toward rate
FAIL_TOWARD_FLOOR = 0.50        # failure gate: toward below this on a future window

# EDGE-RETIRE-001: all six candidates RETIRED on out-of-sample evidence
# (docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md). Retired policies remain in
# the frozen registry for registry/observation purposes but are ineligible
# for any live gate/paper/MVP discussion; resurrection requires a NEW prereg
# document with a NEW lock.
RETIREMENT_DOC = "docs/EDGE_SELECTION_RETIREMENT_2026_07_10.md"
RETIRED_AT = "2026-07-11T07:15:00+00:00"
RETIRED_CANDIDATES: dict[str, dict] = {
    "require_gap_follows_move_totals_only": {
        "discovery": "0.539/+0.42", "validation": "0.286/-1.22"},
    "require_gap_follows_move_exclude_spreads": {
        "discovery": "0.459/+0.26", "validation": "0.261/-1.52"},
    "gap_follows_move_and_high_liquidity": {
        "discovery": "0.483/+0.35", "validation": "0.220/-1.74"},
    "gap_follows_move_and_tight_spread": {
        "discovery": "0.404/+0.24", "validation": "0.232/-1.38"},
    "total_only": {
        "discovery": "0.380/-0.11", "validation": "0.349/-0.06"},
    "exclude_spread_markets": {
        "discovery": "0.335/-0.22", "validation": "0.337/-0.25"},
}
RETIREMENT_CONCLUSION = (
    "the 18-policy search OVERFIT: all six pre-registered candidates failed "
    "their first substantial out-of-sample window (some inverted to far worse "
    "than baseline) while the negative control outperformed them, and the "
    "cost-adjusted view had independently killed every cohort. No retired "
    "policy is eligible for any live gate, paper-trading discussion, or "
    "MVP-005B step; a successor hypothesis requires a NEW prereg + NEW lock "
    "and should be mechanism-first, with cost-adjusted gates from day one."
)

WINDOW_DISCOVERY = "discovery"
WINDOW_VALIDATION = "validation"
WINDOW_MIXED = "mixed"

STATUS_BASELINE = "baseline"
STATUS_VALIDATED = "validated_shadow"
STATUS_DISCOVERY_ONLY = "passes_gates_discovery_only"
STATUS_INSUFFICIENT = "insufficient_sample"
STATUS_INCONCLUSIVE = "inconclusive_continue_observing"
STATUS_FAILING = "failing_gates"
STATUS_SAMPLE_COLLAPSED = "sample_collapsed"
STATUS_CONTROL_CONSISTENT = "control_consistent"
STATUS_CONTROL_ANOMALY = "control_anomaly"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify_window(start: datetime, end: datetime, lock: datetime) -> str:
    """discovery: the whole window is at or before the lock (in-sample);
    validation: the whole window starts at/after the lock (out-of-sample);
    mixed: it straddles the lock — mixed windows can NOT validate."""
    if start >= lock:
        return WINDOW_VALIDATION
    if end <= lock:
        return WINDOW_DISCOVERY
    return WINDOW_MIXED


def evaluate_gates(summary: dict, window_type: str, role: str) -> dict:
    """Deterministic pass/fail per pre-registered gate + an overall status.
    Pure function over one policy's summary — unit-testable without a DB.
    Statuses are protocol states, never advice or authorization."""
    ft60 = summary["follow_through"].get("60m", {})
    n = summary["final_n"]
    toward = ft60.get("moved_toward_rate")
    closure = ft60.get("mean_gap_closure_pct")
    ticker_share = summary.get("max_ticker_share") or 0
    game_share = summary.get("max_game_share") or 0

    gates = {
        "sample_n_ge_75": n >= VALIDATED_MIN_N,
        "sample_n_ge_150_preferred": n >= VALIDATED_PREFERRED_N,
        "toward_60m_ge_0_55": toward is not None and toward >= VALIDATED_TOWARD_60,
        "closure_60m_positive": closure is not None and closure > 0,
        "max_ticker_share_le_0_34": ticker_share <= MAX_TICKER_SHARE,
        "max_game_share_le_0_50": game_share <= MAX_GAME_SHARE,
        "out_of_sample_window": window_type == WINDOW_VALIDATION,
    }
    failure_reasons = []
    if n < MIN_READABLE_FINAL_N:
        failure_reasons.append(f"sample_collapsed final_n={n} < {MIN_READABLE_FINAL_N}")
    if toward is not None and toward < FAIL_TOWARD_FLOOR and n >= MIN_READABLE_FINAL_N:
        failure_reasons.append(f"toward_60m={toward} < {FAIL_TOWARD_FLOOR}")
    if closure is not None and closure < 0 and n >= MIN_READABLE_FINAL_N:
        failure_reasons.append(f"closure_60m={closure} negative")
    if not gates["max_ticker_share_le_0_34"]:
        failure_reasons.append(f"max_ticker_share={ticker_share} > {MAX_TICKER_SHARE}")
    if not gates["max_game_share_le_0_50"]:
        failure_reasons.append(f"max_game_share={game_share} > {MAX_GAME_SHARE}")

    if role == ROLE_BASELINE:
        status, reason = STATUS_BASELINE, "reference population — not a candidate"
    elif role == ROLE_NEGATIVE_CONTROL:
        adverse = (toward is not None and toward < FAIL_TOWARD_FLOOR) or (
            closure is not None and closure < 0
        )
        if n < MIN_READABLE_FINAL_N:
            status, reason = STATUS_INSUFFICIENT, f"control unreadable (final_n={n})"
        elif adverse:
            status, reason = STATUS_CONTROL_CONSISTENT, (
                f"negative control remains adverse as expected "
                f"(toward_60m={toward}, closure_60m={closure})"
            )
        else:
            status, reason = STATUS_CONTROL_ANOMALY, (
                f"NEGATIVE CONTROL TURNED NON-ADVERSE (toward_60m={toward}, "
                f"closure_60m={closure}) — possible regime shift or methodology "
                f"issue; treat all candidate results in this window with suspicion"
            )
    else:  # candidate
        quant_pass = (
            gates["sample_n_ge_75"]
            and gates["toward_60m_ge_0_55"]
            and gates["closure_60m_positive"]
            and gates["max_ticker_share_le_0_34"]
            and gates["max_game_share_le_0_50"]
        )
        if n < MIN_READABLE_FINAL_N:
            status, reason = STATUS_SAMPLE_COLLAPSED, (
                f"final_n={n} < {MIN_READABLE_FINAL_N} — unreadable this window"
            )
        elif failure_reasons:
            status, reason = STATUS_FAILING, "; ".join(failure_reasons)
        elif quant_pass and window_type == WINDOW_VALIDATION:
            status, reason = STATUS_VALIDATED, (
                f"all pre-registered gates pass on an out-of-sample window "
                f"(final_n={n}"
                + ("" if gates["sample_n_ge_150_preferred"]
                   else f" — minimum met, preferred n>={VALIDATED_PREFERRED_N} not yet")
                + "). Protocol status only — MVP-005B still requires explicit "
                  "human acceptance"
            )
        elif quant_pass:
            status, reason = STATUS_DISCOVERY_ONLY, (
                f"quantitative gates pass but this is a {window_type} window — "
                f"the window that selected a policy can never validate it"
            )
        elif not gates["sample_n_ge_75"]:
            status, reason = STATUS_INSUFFICIENT, (
                f"final_n={n} < {VALIDATED_MIN_N} — not failing, not validatable; "
                f"keep observing"
            )
        else:
            status, reason = STATUS_INCONCLUSIVE, (
                f"toward_60m={toward} in [{FAIL_TOWARD_FLOOR}, "
                f"{VALIDATED_TOWARD_60}) or closure marginal — neither passing "
                f"nor failing; keep observing"
            )
    return {"gates": gates, "failure_reasons": failure_reasons,
            "status": status, "status_reason": reason}


class EdgeSelectionValidationReportService:
    """Builds the pre-registered validation report. Read-only; persists
    nothing; evaluates ONLY the frozen registry."""

    def build(
        self,
        session: Session,
        hours: int = 24,
        since: datetime | None = None,
        until: datetime | None = None,
        lock: datetime | None = None,
    ) -> dict:
        lock = lock or PREREG_LOCKED_AT
        now = _now()
        start = _aware(since) if since else now - timedelta(hours=hours)
        end = _aware(until) if until else now
        load_hours = max(1, math.ceil((now - start).total_seconds() / 3600))
        all_rows = EdgeFollowthroughDiagnosticService().build_row_diagnostics(
            session, load_hours
        )
        rows = [r for r in all_rows if start <= _aware(r.created_at) <= end]
        window_type = classify_window(start, end, lock)
        pre_lock = sum(1 for r in rows if _aware(r.created_at) <= lock)

        predicates = dict(POLICIES)
        baseline_n = len(rows)
        results: list[dict] = []
        for name, role, alias in PREREGISTERED:
            predicate = predicates[name]
            included = [r for r in rows if predicate(r, {})]
            excluded = [r for r in rows if not predicate(r, {})]
            summary = summarize_policy(name, included, excluded, baseline_n)
            summary["role"] = role
            summary["prereg_alias"] = alias
            summary.update(evaluate_gates(summary, window_type, role))
            if name in RETIRED_CANDIDATES:
                summary["retired"] = True
                summary["status_reason"] += (
                    " [RETIRED per EDGE-RETIRE-001 — registry observation "
                    "only; ineligible for live gate/paper/MVP]"
                )
            # trim keys this protocol report does not need
            summary.pop("signal_type_mix", None)
            summary.pop("gap_sign_mix", None)
            results.append(summary)

        control = next(r for r in results if r["role"] == ROLE_NEGATIVE_CONTROL)
        validated = [r["name"] for r in results if r["status"] == STATUS_VALIDATED]
        return {
            "note": SELECTION_NOTE,
            "prereg_doc": PREREG_DOC,
            "prereg_locked_at": lock.isoformat(),
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "type": window_type,
                "rows_pre_lock": pre_lock,
                "rows_post_lock": len(rows) - pre_lock,
            },
            "population": baseline_n,
            "policies": results,
            "negative_control_consistent": control["status"] == STATUS_CONTROL_CONSISTENT,
            "validated_shadow_policies": validated,
            "overfitting_note": OVERFITTING_NOTE,
            "mvp_005b_note": MVP_005B_NOTE,
        }
