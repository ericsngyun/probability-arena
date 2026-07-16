"""CRYPTO-HORIZON-SHARED-CANDIDATE-FEASIBILITY-001 — local, read-only feasibility
measurement for the horizon shared-pass canary (CANARY-004).

Answers, from ALREADY-PERSISTED local data only, whether the current discovery /
lifecycle-anchor / cohort-selection pipeline can realistically produce TWO
complete-state tokens whose 15m planner windows overlap early enough to arm a
shared due-now cohort. Pure measurement:

  * zero provider calls (no DexScreener/GoPlus/SolanaTracker/Birdeye)
  * zero writes (no discovery, cohort, observation, unit, or migration)
  * no EV, side, size, order, recommendation, wallet, key, swap, or execution

It reuses the DEPLOYED definitions — `_completeness_reason` (the exact
`--require-complete` rule), `_horizon_windows`, `_shared_windows`, and
`ACTIVATION_GRACE` — so the report can never drift from the selector it measures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CryptoTokenBirthEvent
from app.services.crypto_horizon import (
    _completeness_reason,
    _horizon_windows,
    _shared_windows,
)
from app.services.crypto_horizon_orchestrator import ACTIVATION_GRACE
from app.services.crypto_tape import HORIZONS, _aware, _now

FEASIBILITY_NOTE = (
    "Local read-only shared-candidate feasibility measurement for the horizon "
    "shared-pass canary. Zero provider calls, zero writes; never EV, a side, a "
    "size, an order, a recommendation, a wallet, a key, a swap, or execution."
)

# beyond this anchor separation two 15m windows ([birth+7.5m, birth+22.5m], width
# = 15m) can never intersect, so pairing is bounded to this neighborhood.
DEFAULT_NEIGHBORHOOD_MINUTES = 15
DEFAULT_RANGES = (("24h", timedelta(hours=24)), ("7d", timedelta(days=7)),
                  ("14d", timedelta(days=14)), ("30d", timedelta(days=30)))


# --- pure geometry (independently unit-testable, no I/O) ------------------------


def fifteen_window(anchor: datetime) -> tuple[datetime, datetime]:
    """[open, close] of the 15m planner window, reusing the deployed window math."""
    row = _horizon_windows(anchor, anchor)[0]  # index 0 == "15m"
    assert row["horizon"] == "15m"
    return row["_start"], row["_end"]


def safe_arm_deadline(close_15m: datetime, margin_seconds: float = 0.0) -> datetime:
    """Latest instant a still-closing 15m window can be armed: the window close
    minus the activation grace (deployed constant) minus any operator/install/
    verification margin. A candidate must be PERSISTED at or before this to be
    usable for a due-now arm."""
    return close_15m - ACTIVATION_GRACE - timedelta(seconds=max(0.0, margin_seconds))


def shared_fifteen(anchor_a: datetime, anchor_b: datetime) -> tuple[datetime, datetime] | None:
    """Intersection of two 15m windows, or None when they do not overlap."""
    ao, ac = fifteen_window(anchor_a)
    bo, bc = fifteen_window(anchor_b)
    lo, hi = max(ao, bo), min(ac, bc)
    return (lo, hi) if lo <= hi else None


def pair_feasibility(
    anchor_a: datetime, persist_a: datetime,
    anchor_b: datetime, persist_b: datetime,
    now: datetime, *, margin_seconds: float = 0.0,
) -> dict:
    """Full two-token shared-pass feasibility using the deployed `_shared_windows`
    for every horizon plus persistence-timing usability for the 15m arm."""
    wa = _horizon_windows(anchor_a, now)
    wb = _horizon_windows(anchor_b, now)
    shared = _shared_windows([wa, wb], now)
    inter = shared_fifteen(anchor_a, anchor_b)
    overlap = inter is not None
    grace_fits = bool(shared["activation_grace_fits_shared_window"])
    latest_arm = safe_arm_deadline(inter[1], margin_seconds) if inter else None
    both_persisted = max(persist_a, persist_b)
    persisted_in_time = bool(latest_arm and both_persisted <= latest_arm)
    return {
        "anchor_delta_seconds": round(abs((anchor_b - anchor_a).total_seconds()), 3),
        "overlap": overlap,
        "grace_fits": grace_fits,
        "shared_pass_eligible": bool(shared["shared_pass_eligible"]),
        "shared_15m_open": inter[0] if inter else None,
        "shared_15m_close": inter[1] if inter else None,
        "earliest_safe_cohort_creation": inter[0] if inter else None,
        "latest_safe_arm": latest_arm,
        "both_persisted_at": both_persisted,
        "usable": bool(persisted_in_time and grace_fits and shared["shared_pass_eligible"]),
    }


# --- funnel ---------------------------------------------------------------------


@dataclass
class _Anchor:
    token: str
    symbol: str | None
    source: str | None
    dex: str | None
    anchor: datetime | None
    persist: datetime | None
    birth: CryptoTokenBirthEvent


def _load_anchors(session: Session, chain: str) -> list[_Anchor]:
    rows = session.execute(
        select(CryptoTokenBirthEvent).where(CryptoTokenBirthEvent.chain == chain)
    ).scalars().all()
    out: list[_Anchor] = []
    for b in rows:
        out.append(_Anchor(
            token=b.token_address, symbol=b.symbol, source=b.launch_source,
            dex=b.first_dex_id,
            anchor=_aware(b.first_evidence_at) or _aware(b.observed_at),
            persist=_aware(b.created_at), birth=b,
        ))
    return out


def _funnel(anchors: list[_Anchor], min_liquidity: float, margin_seconds: float) -> dict:
    n_all = len(anchors)
    valid_fe = [a for a in anchors if a.anchor]
    has_pair = [a for a in valid_fe if a.birth.first_pair_address]
    has_price = [a for a in has_pair if a.birth.initial_price_usd is not None]
    pos_liq = [a for a in has_price
               if a.birth.initial_liquidity_usd is not None and a.birth.initial_liquidity_usd > 0]
    complete = [a for a in valid_fe if _completeness_reason(a.birth, min_liquidity) is None]
    feasible_at_persist, armable = [], []
    for a in complete:
        if not a.persist:
            continue
        _open, close = fifteen_window(a.anchor)
        if a.persist <= close:
            feasible_at_persist.append(a)
        if a.persist <= safe_arm_deadline(close, margin_seconds):
            armable.append(a)
    reasons: dict[str, int] = {}
    for a in valid_fe:
        r = _completeness_reason(a.birth, min_liquidity) or "complete"
        reasons[r] = reasons.get(r, 0) + 1

    def step(name, n):
        return {"step": name, "count": n, "pct_of_all": round(100.0 * n / n_all, 2) if n_all else 0.0}

    return {
        "denominator": n_all,
        "steps": [
            step("all_token_anchors", n_all),
            step("valid_first_evidence_at", len(valid_fe)),
            step("deterministic_initial_pair", len(has_pair)),
            step("initial_price_present", len(has_price)),
            step("positive_initial_liquidity", len(pos_liq)),
            step("complete_state_eligible", len(complete)),
            step("persisted_while_15m_feasible", len(feasible_at_persist)),
            step("persisted_with_safe_arm_margin", len(armable)),
        ],
        "completeness_failure_reasons": dict(sorted(reasons.items(), key=lambda x: -x[1])),
    }


def _segment(anchors: list[_Anchor], key, min_liquidity: float, margin_seconds: float, limit: int) -> list[dict]:
    groups: dict = {}
    for a in anchors:
        groups.setdefault(key(a), []).append(a)
    out = []
    for name, rs in groups.items():
        complete = [a for a in rs if a.anchor and _completeness_reason(a.birth, min_liquidity) is None]
        null_liq = [a for a in rs
                    if a.birth.initial_liquidity_usd is None or a.birth.initial_liquidity_usd <= 0]
        lags = sorted((a.persist - a.anchor).total_seconds()
                      for a in complete if a.persist and a.anchor)
        armable = 0
        for a in complete:
            if a.persist and a.persist <= safe_arm_deadline(fifteen_window(a.anchor)[1], margin_seconds):
                armable += 1
        out.append({
            "name": name, "n": len(rs), "complete": len(complete),
            "complete_rate": round(len(complete) / len(rs), 3) if rs else None,
            "null_liquidity": len(null_liq),
            "null_liquidity_rate": round(len(null_liq) / len(rs), 3) if rs else None,
            "median_lag_seconds_complete": lags[len(lags) // 2] if lags else None,
            "armable_complete": armable,
        })
    out.sort(key=lambda d: -d["n"])
    return out[:limit]


def _shared_window_analysis(
    anchors: list[_Anchor], now: datetime, *, neighborhood_minutes: int,
    min_liquidity: float, margin_seconds: float, limit: int,
) -> dict:
    comp = sorted(
        [a for a in anchors if a.anchor and a.persist
         and _completeness_reason(a.birth, min_liquidity) is None],
        key=lambda a: a.anchor,
    )
    total = overlap = grace = usable = 0
    examples: list[dict] = []
    moments: list[datetime] = []
    usable_days: set = set()
    horizon = timedelta(minutes=neighborhood_minutes)
    for i in range(len(comp)):
        a = comp[i]
        for j in range(i + 1, len(comp)):
            b = comp[j]
            if b.anchor - a.anchor > horizon:
                break
            total += 1
            pf = pair_feasibility(a.anchor, a.persist, b.anchor, b.persist, now,
                                  margin_seconds=margin_seconds)
            if pf["overlap"]:
                overlap += 1
            if pf["grace_fits"] and pf["overlap"]:
                grace += 1
            if pf["usable"]:
                usable += 1
                usable_days.add(pf["both_persisted_at"].date().isoformat())
                lo = pf["shared_15m_open"]
                if not any(abs((lo - m).total_seconds()) < neighborhood_minutes * 60 for m in moments):
                    moments.append(lo)
                if len(examples) < limit:
                    examples.append({
                        "token_a": a.symbol, "token_b": b.symbol,
                        "anchor_delta_seconds": pf["anchor_delta_seconds"],
                        "shared_15m_open": pf["shared_15m_open"].isoformat(),
                        "shared_15m_close": pf["shared_15m_close"].isoformat(),
                        "latest_safe_arm": pf["latest_safe_arm"].isoformat(),
                        "both_persisted_at": pf["both_persisted_at"].isoformat(),
                        "arm_slack_seconds": round(
                            (pf["latest_safe_arm"] - pf["both_persisted_at"]).total_seconds(), 1),
                    })
    return {
        "neighborhood_minutes": neighborhood_minutes,
        "activation_grace_seconds": ACTIVATION_GRACE.total_seconds(),
        "operator_arm_margin_seconds": margin_seconds,
        "complete_pairs_in_neighborhood": total,
        "overlapping_15m_windows": overlap,
        "grace_compatible_shared_windows": grace,
        "usable_pairs_persisted_in_time": usable,
        "distinct_usable_moments": len(moments),
        "days_with_usable_pair": sorted(usable_days),
        "examples": examples,
    }


def build_feasibility_report(
    session: Session, *, now: datetime | None = None, chain: str = "solana",
    ranges: tuple[tuple[str, timedelta], ...] = DEFAULT_RANGES,
    neighborhood_minutes: int = DEFAULT_NEIGHBORHOOD_MINUTES,
    min_liquidity: float = 0.0, arm_margin_seconds: float = 0.0, limit: int = 10,
) -> dict:
    """Assemble the full feasibility report. Read-only; zero external calls."""
    now = _aware(now) or _now()
    anchors = _load_anchors(session, chain)
    fe = [a.anchor for a in anchors if a.anchor]
    persists = [a.persist for a in anchors if a.persist]
    coverage = {
        "total_anchors": len(anchors),
        "first_evidence_min": min(fe).isoformat() if fe else None,
        "first_evidence_max": max(fe).isoformat() if fe else None,
        "persist_min": min(persists).isoformat() if persists else None,
        "persist_max": max(persists).isoformat() if persists else None,
        "anchor_span_days": round((max(fe) - min(fe)).total_seconds() / 86400, 2) if fe else None,
        "requested_ranges": [],
    }
    span_days = coverage["anchor_span_days"] or 0.0

    funnels = {}
    for label, delta in ranges:
        cutoff = now - delta
        subset = [a for a in anchors if a.anchor and a.anchor >= cutoff]
        covered = delta.total_seconds() / 86400 <= span_days + 1e-9
        coverage["requested_ranges"].append({
            "range": label, "cutoff_utc": cutoff.isoformat(),
            "fully_covered_by_history": covered,
            "note": None if covered else "range exceeds available history; counts are the full available window",
        })
        funnels[label] = _funnel(subset, min_liquidity, arm_margin_seconds)

    return {
        "status": "ok",
        "note": FEASIBILITY_NOTE,
        "external_calls": 0,
        "persisted": False,
        "writes": 0,
        "generated_at": now.isoformat(),
        "now_utc": now.isoformat(),
        "chain": chain,
        "min_liquidity": min_liquidity,
        "activation_grace_seconds": ACTIVATION_GRACE.total_seconds(),
        "operator_arm_margin_seconds": arm_margin_seconds,
        "history_coverage": coverage,
        "funnels": funnels,
        "shared_window": _shared_window_analysis(
            anchors, now, neighborhood_minutes=neighborhood_minutes,
            min_liquidity=min_liquidity, margin_seconds=arm_margin_seconds, limit=limit),
        "by_launch_source": _segment(anchors, lambda a: a.source, min_liquidity, arm_margin_seconds, limit),
        "by_pair_venue": _segment(anchors, lambda a: a.dex, min_liquidity, arm_margin_seconds, limit),
    }
