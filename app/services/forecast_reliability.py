"""FORECAST-RELIABILITY-DECOMP-001 — read-only calibration/reliability decomposition
over CURRENT, VALID scored forecasts only.

Answers whether forecast probabilities are empirically calibrated, whether they beat
simple non-model Brier baselines, how calibration error decomposes (Murphy /
reliability–resolution–uncertainty), whether over/underconfidence dominates, whether
reliability differs by cohort, and whether it is improving/stable/deteriorating over
time — with composition-shift and thin-sample guards.

Forecast-measurement infrastructure only. It **reuses** the FORECAST-SCORABILITY-AUDIT-001
canonical current-state population (`forecast_scorability.classify_forecast` /
`SCORED_CURRENT` / `_load_rows` / `_representation`) and the deployed `calibration.
brier_score` / `log_loss` — it does not reinvent current-score semantics, does not
alter a forecast/outcome/score, changes no calibration gate or MarketOps behavior,
writes nothing, calls no provider. No EV, side, size, order, recommendation, wallet, or
trading output exists by construction. "Skill" is the standard forecast-scoring term,
never financial edge/profit/return/actionability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.services.calibration import brier_score, log_loss
from app.services.forecast_scorability import (
    GROUP_INCONSISTENT,
    GROUP_PENDING,
    GROUP_SCORABLE_BACKLOG,
    GROUP_SCORED,
    GROUP_STALE_BACKLOG,
    GROUP_UNSCORABLE,
    SCORED_CURRENT,
    _aware,
    _completeness_bucket,
    _group_counts,
    _load_rows,
    _representation,
)

DISCLAIMER = (
    "Forecast-reliability measurement only — never advice, EV, a side, a size, an "
    "order, a recommendation, a wallet, or a trade action. 'Skill' is a forecast-"
    "scoring term (Brier skill score), never financial edge/profit/return."
)

DEFAULT_BINS = 10
NEUTRAL_P = 0.50
CALIB_TOLERANCE = 0.05          # |gap| within this -> approximately_calibrated
MIN_BIN_MEASURED = 10           # bin sample floor to be 'measured'
MIN_BIN_DESCRIPTIVE = 3         # below this a bin is 'too_thin'
MIN_SCORED = 30                 # below this -> INSUFFICIENT_RELIABILITY_DATA
MIN_COHORT = 20                 # cohort sample floor
MIN_PERIOD = 15                 # per-period sample floor for trend
MIN_TREND_PERIODS = 4           # periods meeting floor required to call a trend
ECE_HIGH = 0.10                 # ECE at/above this -> reliability error is material
REL_DOMINATE_FRAC = 0.25        # reliability / actual_brier at/above -> dominates
RES_WEAK_FRAC = 0.10            # resolution / uncertainty at/below -> weak resolution
DIR_DOMINATE_SHARE = 0.50       # weighted share of over/underconfident bins to dominate
HETERO_SKILL_SPREAD = 0.30      # max-min cohort base-rate skill spread -> heterogeneity
TREND_ECE_TOL = 0.02            # early-vs-late ECE delta below this -> stable
COMP_SHIFT_PP = 20.0            # top-cohort mix delta (pp) early-vs-late -> comp shift
COMP_SHIFT_PREV = 0.15          # prevalence delta early-vs-late -> comp shift

# directional calibration states
OVERCONF_POS = "overconfident_positive"
UNDERCONF_POS = "underconfident_positive"
OVERCONF_NEG = "overconfident_negative"
UNDERCONF_NEG = "underconfident_negative"
APPROX_CALIBRATED = "approximately_calibrated"

VERDICTS = (
    "INSUFFICIENT_RELIABILITY_DATA", "RELIABILITY_SAMPLE_NOT_REPRESENTATIVE",
    "BASE_RATE_BASELINE_NOT_BEATEN", "RELIABILITY_ERROR_DOMINATES", "RESOLUTION_IS_WEAK",
    "OVERCONFIDENCE_DOMINATES", "UNDERCONFIDENCE_DOMINATES", "DOMAIN_HETEROGENEITY_DOMINATES",
    "COMPOSITION_SHIFT_DOMINATES", "RELIABILITY_STABLE", "RELIABILITY_IMPROVING",
    "RELIABILITY_DETERIORATING", "MULTIPLE_RELIABILITY_FINDINGS",
)


@dataclass
class _Point:
    p: float
    y: float
    created_at: datetime
    domain: str
    forecaster: str
    evidence_depth: str
    forecast_risk: str
    research_completeness_bucket: str
    research_risk: str
    resolution_risk: str
    tradeability: str


# --- pure helpers (independently testable) --------------------------------------


def make_edges(bins: int) -> list[float]:
    if not isinstance(bins, int) or bins < 2:
        raise ValueError("bins must be an integer >= 2")
    return [round(i / bins, 10) for i in range(bins + 1)]


def bin_index(p: float, edges: list[float]) -> int:
    """[lo, hi) for every bin except the last, which is [lo, hi]. p is a probability
    in [0,1]."""
    if p < edges[0] or p > edges[-1]:
        raise ValueError(f"probability {p} outside [0,1]")
    if p >= edges[-2]:            # last bin is closed on the right (includes 1.0)
        return len(edges) - 2
    lo = 0
    for k in range(len(edges) - 1):
        if edges[k] <= p < edges[k + 1]:
            lo = k
            break
    return lo


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n == 0:
        return (None, None)
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return (round(max(0.0, center - margin), 4), round(min(1.0, center + margin), 4))


def direction(mean_p: float, obs_rate: float, tol: float = CALIB_TOLERANCE) -> str:
    """Precise directional calibration classification."""
    gap = mean_p - obs_rate
    if abs(gap) <= tol:
        return APPROX_CALIBRATED
    if mean_p >= 0.5:
        return OVERCONF_POS if gap > 0 else UNDERCONF_POS
    return OVERCONF_NEG if gap < 0 else UNDERCONF_NEG


def _bin_label(n: int) -> str:
    if n < MIN_BIN_DESCRIPTIVE:
        return "too_thin"
    if n < MIN_BIN_MEASURED:
        return "descriptive_only"
    return "measured"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def compute_bins(points: list[_Point], edges: list[float]) -> list[dict]:
    n_total = len(points)
    buckets: dict[int, list[_Point]] = {}
    for pt in points:
        buckets.setdefault(bin_index(pt.p, edges), []).append(pt)
    out = []
    for k in range(len(edges) - 1):
        pts = buckets.get(k, [])
        n = len(pts)
        yes = sum(1 for pt in pts if pt.y == 1.0)
        mean_p = _mean([pt.p for pt in pts])
        obs = (yes / n) if n else None
        gap = (mean_p - obs) if (mean_p is not None and obs is not None) else None
        out.append({
            "bin": k, "lower": edges[k], "upper": edges[k + 1],
            "inclusion": "[lo,hi]" if k == len(edges) - 2 else "[lo,hi)",
            "count": n, "share": round(n / n_total, 4) if n_total else 0.0,
            "mean_forecast_probability": round(mean_p, 4) if mean_p is not None else None,
            "observed_positive_rate": round(obs, 4) if obs is not None else None,
            "calibration_gap": round(gap, 6) if gap is not None else None,
            "abs_calibration_gap": round(abs(gap), 6) if gap is not None else None,
            "mean_brier": round(_mean([brier_score(pt.p, pt.y) for pt in pts]), 6) if n else None,
            "mean_log_loss": round(_mean([log_loss(pt.p, pt.y) for pt in pts]), 6) if n else None,
            "yes": yes, "no": n - yes,
            "observed_rate_wilson95": wilson_interval(yes, n),
            "direction": direction(mean_p, obs) if (mean_p is not None and obs is not None) else None,
            "label": _bin_label(n),
        })
    return out


def calibration_error(bin_stats: list[dict], n_total: int) -> dict:
    populated = [b for b in bin_stats if b["count"] > 0]
    measured = [b for b in populated if b["label"] == "measured"]
    # ECE/MCE sum the 6dp per-bin gaps (well below the 4dp report precision -> exact
    # at report precision, not the ECE of doubly-rounded values).
    ece = sum(b["count"] / n_total * b["abs_calibration_gap"] for b in populated) if n_total else 0.0
    mce_from_measured = bool(measured)
    mce_pool = measured or populated
    mce = max((b["abs_calibration_gap"] for b in mce_pool), default=None)
    return {
        "ece": round(ece, 4), "mce": round(mce, 4) if mce is not None else None,
        # disclose whether MCE came from a measured bin or fell back to a too-thin one
        "mce_source": ("measured" if mce_from_measured else "fallback_populated") if mce is not None else None,
        "populated_bins": len(populated), "measured_bins": len(measured),
        "too_thin_bins": len(populated) - len(measured),
        "minimum_bin_count": MIN_BIN_MEASURED,
    }


def baselines(points: list[_Point]) -> dict:
    n = len(points)
    prev = _mean([pt.y for pt in points]) if n else None
    model = _mean([brier_score(pt.p, pt.y) for pt in points]) if n else None
    neutral = _mean([brier_score(NEUTRAL_P, pt.y) for pt in points]) if n else None
    base = _mean([brier_score(prev, pt.y) for pt in points]) if (n and prev is not None) else None

    def skill(m, b):
        if m is None or b is None or b == 0.0:
            return None
        return round(1.0 - m / b, 4)

    return {
        "sample_size": n, "prevalence": round(prev, 4) if prev is not None else None,
        "model_brier": round(model, 6) if model is not None else None,
        "neutral_baseline_brier": round(neutral, 6) if neutral is not None else None,
        "base_rate_baseline_brier": round(base, 6) if base is not None else None,
        "abs_diff_vs_base_rate": (round(abs(model - base), 6)
                                  if (model is not None and base is not None) else None),
        "brier_skill_vs_neutral": skill(model, neutral),
        "brier_skill_vs_base_rate": skill(model, base),
        "base_rate_zero_variance": (base == 0.0) if base is not None else None,
    }


def murphy_decomposition(points: list[_Point], edges: list[float]) -> dict:
    n = len(points)
    if n == 0:
        return {"reliability": None, "resolution": None, "uncertainty": None,
                "reconstructed_brier": None, "actual_brier": None,
                "discretization_residual": None, "bins": len(edges) - 1, "populated_bins": 0,
                "sample_size": 0}
    prev = _mean([pt.y for pt in points])
    uncertainty = prev * (1 - prev)
    buckets: dict[int, list[_Point]] = {}
    for pt in points:
        buckets.setdefault(bin_index(pt.p, edges), []).append(pt)
    reliability = resolution = 0.0
    for pts in buckets.values():
        nk = len(pts)
        pbar = _mean([pt.p for pt in pts])
        ok = _mean([pt.y for pt in pts])
        reliability += nk / n * (pbar - ok) ** 2
        resolution += nk / n * (ok - prev) ** 2
    actual = _mean([brier_score(pt.p, pt.y) for pt in points])
    reconstructed = reliability - resolution + uncertainty
    return {
        "reliability": round(reliability, 6), "resolution": round(resolution, 6),
        "uncertainty": round(uncertainty, 6), "reconstructed_brier": round(reconstructed, 6),
        "actual_brier": round(actual, 6),
        "discretization_residual": round(actual - reconstructed, 6),
        "bins": len(edges) - 1, "populated_bins": len(buckets), "sample_size": n,
    }


def directional_summary(bin_stats: list[dict], points: list[_Point]) -> dict:
    n = len(points)
    prev = _mean([pt.y for pt in points]) if n else None
    signed = ((_mean([pt.p for pt in points]) - prev) if (n and prev is not None) else None)
    over_w = under_w = 0.0
    over_share = under_share = 0.0
    for b in bin_stats:
        if b["count"] == 0 or b["direction"] is None:
            continue
        frac = b["count"] / n
        if b["direction"] in (OVERCONF_POS, OVERCONF_NEG):
            over_w += frac
            over_share += frac
        elif b["direction"] in (UNDERCONF_POS, UNDERCONF_NEG):
            under_w += frac
            under_share += frac
    extreme_miss = sum(1 for pt in points if (pt.p >= 0.9 and pt.y == 0.0) or (pt.p < 0.1 and pt.y == 1.0))
    high_correct = sum(1 for pt in points if (pt.p >= 0.9 and pt.y == 1.0) or (pt.p < 0.1 and pt.y == 0.0))
    return {
        "signed_calibration_gap": round(signed, 4) if signed is not None else None,
        "abs_calibration_gap": round(abs(signed), 4) if signed is not None else None,
        "overprediction_weighted_share": round(over_share, 4),
        "underprediction_weighted_share": round(under_share, 4),
        "extreme_confidence_miss_count": extreme_miss,
        "high_confidence_correct_count": high_correct,
        "_over_w": over_w, "_under_w": under_w,
    }


def _cohort_reliability(points: list[_Point], key, edges: list[float], all_scored: int,
                        limit: int) -> list[dict]:
    groups: dict = {}
    for pt in points:
        groups.setdefault(key(pt), []).append(pt)
    out = []
    for name, pts in groups.items():
        b = baselines(pts)
        binstats = compute_bins(pts, edges)
        ce = calibration_error(binstats, len(pts))
        out.append({
            "name": str(name), "scored_count": len(pts), "prevalence": b["prevalence"],
            "mean_brier": b["model_brier"], "neutral_baseline_brier": b["neutral_baseline_brier"],
            "base_rate_baseline_brier": b["base_rate_baseline_brier"],
            "brier_skill_vs_base_rate": b["brier_skill_vs_base_rate"],
            "ece": ce["ece"], "mce": ce["mce"], "populated_bins": ce["populated_bins"],
            "representation_share": round(len(pts) / all_scored, 4) if all_scored else 0.0,
            "sample_label": "measured" if len(pts) >= MIN_COHORT else "too_thin",
        })
    out.sort(key=lambda d: (-d["scored_count"], d["name"]))
    return out[:limit]


def _iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _period_stats(points: list[_Point], key, edges: list[float]) -> list[dict]:
    groups: dict = {}
    for pt in points:
        groups.setdefault(key(pt.created_at), []).append(pt)
    out = []
    for name in sorted(groups):
        pts = groups[name]
        b = baselines(pts)
        ce = calibration_error(compute_bins(pts, edges), len(pts))
        dom = {}
        fc = {}
        for pt in pts:
            dom[pt.domain] = dom.get(pt.domain, 0) + 1
            fc[pt.forecaster] = fc.get(pt.forecaster, 0) + 1
        top_dom = max(dom.values()) / len(pts)
        top_fc = max(fc.values()) / len(pts)
        out.append({
            "period": name, "scored_count": len(pts), "prevalence": b["prevalence"],
            "mean_brier": b["model_brier"], "base_rate_baseline_brier": b["base_rate_baseline_brier"],
            "brier_skill_vs_base_rate": b["brier_skill_vs_base_rate"], "ece": ce["ece"],
            "top_domain_share": round(top_dom, 3), "top_forecaster_share": round(top_fc, 3),
            "domain_mix": dom, "prevalence_raw": b["prevalence"],
            "sample_label": "measured" if len(pts) >= MIN_PERIOD else "too_thin",
        })
    return out


def _trend(weekly: list[dict]) -> dict:
    measured = [p for p in weekly if p["scored_count"] >= MIN_PERIOD]
    if len(measured) < MIN_TREND_PERIODS:
        return {"label": "too_thin_for_trend", "measured_periods": len(measured)}
    half = len(measured) // 2
    early, late = measured[:half], measured[half:]

    def avg(rows, k):
        vals = [r[k] for r in rows if r[k] is not None]
        return sum(vals) / len(vals) if vals else None

    early_ece, late_ece = avg(early, "ece"), avg(late, "ece")
    # composition shift: material change in top-domain share or prevalence early->late
    early_dom = avg(early, "top_domain_share")
    late_dom = avg(late, "top_domain_share")
    early_prev = avg(early, "prevalence")
    late_prev = avg(late, "prevalence")
    comp_shift = (
        (early_dom is not None and late_dom is not None
         and abs(late_dom - early_dom) * 100 >= COMP_SHIFT_PP)
        or (early_prev is not None and late_prev is not None
            and abs(late_prev - early_prev) >= COMP_SHIFT_PREV))
    if comp_shift:
        label = "composition_shift_dominates"
    elif early_ece is None or late_ece is None:
        label = "too_thin_for_trend"
    elif abs(late_ece - early_ece) < TREND_ECE_TOL:
        label = "reliability_stable"
    elif late_ece < early_ece:
        label = "reliability_improving"
    else:
        label = "reliability_deteriorating"
    return {
        "label": label, "measured_periods": len(measured),
        "early_mean_ece": round(early_ece, 4) if early_ece is not None else None,
        "late_mean_ece": round(late_ece, 4) if late_ece is not None else None,
        "early_top_domain_share": round(early_dom, 3) if early_dom is not None else None,
        "late_top_domain_share": round(late_dom, 3) if late_dom is not None else None,
        "early_prevalence": round(early_prev, 4) if early_prev is not None else None,
        "late_prevalence": round(late_prev, 4) if late_prev is not None else None,
    }


def _verdict(scored_n, baselines_d, murphy, ce, direction_d, cohorts, trend, strong_skew):
    if scored_n < MIN_SCORED:
        return {"primary": "INSUFFICIENT_RELIABILITY_DATA", "findings": [],
                "reason": f"only {scored_n} scored_current (<{MIN_SCORED})"}
    findings = []
    skill = baselines_d["brier_skill_vs_base_rate"]
    if skill is not None and skill <= 0.0:
        findings.append("BASE_RATE_BASELINE_NOT_BEATEN")
    actual = murphy["actual_brier"] or 0.0
    if (actual > 0 and murphy["reliability"] is not None
            and (murphy["reliability"] / actual >= REL_DOMINATE_FRAC or ce["ece"] >= ECE_HIGH)):
        findings.append("RELIABILITY_ERROR_DOMINATES")
    if (murphy["uncertainty"] and murphy["resolution"] is not None
            and murphy["uncertainty"] > 0
            and murphy["resolution"] / murphy["uncertainty"] <= RES_WEAK_FRAC):
        findings.append("RESOLUTION_IS_WEAK")
    if direction_d["_over_w"] >= DIR_DOMINATE_SHARE and direction_d["_over_w"] > direction_d["_under_w"]:
        findings.append("OVERCONFIDENCE_DOMINATES")
    elif direction_d["_under_w"] >= DIR_DOMINATE_SHARE and direction_d["_under_w"] > direction_d["_over_w"]:
        findings.append("UNDERCONFIDENCE_DOMINATES")
    dom_skills = [c["brier_skill_vs_base_rate"] for c in cohorts.get("domain", [])
                  if c["sample_label"] == "measured" and c["brier_skill_vs_base_rate"] is not None]
    if len(dom_skills) >= 2 and (max(dom_skills) - min(dom_skills)) >= HETERO_SKILL_SPREAD:
        findings.append("DOMAIN_HETEROGENEITY_DOMINATES")
    if trend["label"] == "composition_shift_dominates":
        findings.append("COMPOSITION_SHIFT_DOMINATES")

    if strong_skew:
        # representativeness gates a clean healthy/beaten call
        primary = "RELIABILITY_SAMPLE_NOT_REPRESENTATIVE"
    elif len(findings) >= 2:
        primary = "MULTIPLE_RELIABILITY_FINDINGS"
    elif len(findings) == 1:
        primary = findings[0]
    elif trend["label"] == "reliability_improving":
        primary = "RELIABILITY_IMPROVING"
    elif trend["label"] == "reliability_deteriorating":
        primary = "RELIABILITY_DETERIORATING"
    else:
        primary = "RELIABILITY_STABLE"
    return {"primary": primary, "findings": findings, "reason": "see components/rates"}


def build_reliability_report(
    session: Session, *, now: datetime | None = None, since: datetime | None = None,
    until: datetime | None = None, hours: int | None = None, domain: str | None = None,
    forecaster: str | None = None, bins: int = DEFAULT_BINS,
    minimum_bin_count: int | None = None, minimum_cohort_count: int | None = None,
    top: int = 10,
) -> dict:
    now = _aware(now) or datetime.now(timezone.utc)
    until = _aware(until) or now
    if since is None and hours is not None:
        since = until - timedelta(hours=hours)
    since = _aware(since)
    if since is not None and since > until:
        raise ValueError("invalid window: since is after until")
    edges = make_edges(bins)  # raises on invalid bins

    rows, domain_dropped = _load_rows(
        session, now=now, since=since, until=until, domain=domain, forecaster=forecaster)
    groups = _group_counts(rows)
    scored_rows = [r for r in rows if r.state == SCORED_CURRENT]

    points = []
    for r in scored_rows:
        y = 1.0 if r.outcome.winning_side == "yes" else 0.0
        points.append(_Point(
            p=r.forecast.estimated_probability, y=y, created_at=_aware(r.forecast.created_at),
            domain=r.domain, forecaster=r.forecaster, evidence_depth=r.evidence_depth,
            forecast_risk=r.forecast_risk,
            research_completeness_bucket=_completeness_bucket(r.research_completeness),
            research_risk=r.research_risk, resolution_risk=r.resolution_risk,
            tradeability=r.tradeability))

    population = {
        "all_forecasts": len(rows), "scored_current": groups[GROUP_SCORED],
        "excluded_pending": groups[GROUP_PENDING], "excluded_unscorable": groups[GROUP_UNSCORABLE],
        "excluded_backlog": groups[GROUP_SCORABLE_BACKLOG], "excluded_stale": groups[GROUP_STALE_BACKLOG],
        "excluded_inconsistent": groups[GROUP_INCONSISTENT],
    }

    bin_stats = compute_bins(points, edges)
    ce = calibration_error(bin_stats, len(points))
    base = baselines(points)
    murphy = murphy_decomposition(points, edges)
    direction_d = directional_summary(bin_stats, points)
    all_scored = len(points)
    cohorts = {
        "domain": _cohort_reliability(points, lambda p: p.domain, edges, all_scored, top),
        "forecaster": _cohort_reliability(points, lambda p: p.forecaster, edges, all_scored, top),
        "evidence_depth": _cohort_reliability(points, lambda p: p.evidence_depth, edges, all_scored, top),
        "forecast_risk": _cohort_reliability(points, lambda p: p.forecast_risk, edges, all_scored, top),
        "research_completeness": _cohort_reliability(
            points, lambda p: p.research_completeness_bucket, edges, all_scored, top),
        "research_risk": _cohort_reliability(points, lambda p: p.research_risk, edges, all_scored, top),
        "resolution_risk": _cohort_reliability(points, lambda p: p.resolution_risk, edges, all_scored, top),
        "tradeability": _cohort_reliability(points, lambda p: p.tradeability, edges, all_scored, top),
    }
    # representation reconciled with the scorability audit (same _load_rows + _representation)
    representation = {
        "domain": _representation(rows, scored_rows, lambda r: r.domain, top),
        "evidence_depth": _representation(rows, scored_rows, lambda r: r.evidence_depth, top),
        "forecaster": _representation(rows, scored_rows, lambda r: r.forecaster, top),
    }
    strong_skew = any(c["label"].startswith("strongly_")
                      for dim in representation.values() for c in dim)

    daily = _period_stats(points, lambda dt: dt.date().isoformat(), edges)
    weekly = _period_stats(points, _iso_week, edges)
    trend = _trend(weekly)
    verdict = _verdict(all_scored, base, murphy, ce, direction_d, cohorts, trend, strong_skew)

    direction_pub = {k: v for k, v in direction_d.items() if not k.startswith("_")}
    return {
        "status": "ok", "disclaimer": DISCLAIMER, "external_calls": 0, "persisted": False,
        "writes": 0, "generated_at": now.isoformat(),
        "window": {"since_utc": since.isoformat() if since else None, "until_utc": until.isoformat(),
                   "hours": hours, "domain_filter": domain, "forecaster_filter": forecaster,
                   "domain_filtered_out": domain_dropped, "bins": bins},
        "population": population,
        "reliability_bins": bin_stats,
        "calibration_error": ce,
        "baselines": base,
        "murphy_decomposition": murphy,
        "directional": direction_pub,
        "cohorts": cohorts,
        "representation": representation,
        "temporal": {"daily": daily, "weekly": weekly, "trend": trend},
        "verdict": verdict,
    }
