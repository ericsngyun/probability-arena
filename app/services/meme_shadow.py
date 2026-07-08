"""MEME-SHADOW-001: read-only follow-through / calibration analysis for the
MEME-MAS `review_priority` labels.

It answers ONE measurement question: do the deterministic MEME-MAS labels
(high_review / elevated_review / monitor / reject_risk) actually predict later
TOKEN BEHAVIOR? It reconstructs the label at each historical attention snapshot
(reusing the MEME-MAS agents, with the risk assessment as-of that moment), then
measures how the SAME token moved afterwards using the token's later snapshots.

This is MARKET-MOVEMENT MEASUREMENT, exactly like the edge follow-through
analysis — NOT PnL, NOT paper trading, NOT EV, NOT a trade recommendation, NOT
position sizing. `price_change` is a measured percentage move of the token, not
a simulated fill or profit. review_priority remains a human-review label; this
layer only reports whether that label separates outcomes and whether it needs
recalibration. Everything is derived read-only on demand from persisted rows —
no persistence, no external call, no provider-budget impact. See
docs/SAFETY_BOUNDARIES.md.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CryptoTokenRiskAssessment,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
)
from app.services.meme_mas import (
    DEFAULT_PROFILE,
    REVIEW_PRIORITIES,
    CalibrationProfile,
    MemeMasDiagnosticService,
    TokenInputs,
)

# (label, minutes) — mirrors the meme-news scan cadence tolerance below
HORIZONS: tuple[tuple[str, int], ...] = (
    ("5m", 5), ("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440)
)
# a later snapshot counts for a horizon if within +/- this fraction of it
HORIZON_TOLERANCE = 0.5
# below this a cohort is too_thin to read (mirrors edge-cohort)
MIN_COHORT_SAMPLES = 12
# liquidity below this fraction of the anchor (or a severe/rug flag) => not survived
SURVIVAL_LIQUIDITY_FRACTION = 0.3
# review-priority separation needed to call the labels calibrated (survival delta)
SEPARATION_SURVIVAL_DELTA = 0.10

NOTE = (
    "Read-only follow-through MEASUREMENT of MEME-MAS review_priority labels. "
    "price/liquidity/volume changes are measured market movement of the token — "
    "NOT PnL, NOT a fill, NOT EV, NOT a trade recommendation, NOT position sizing. "
    "review_priority stays a human-review label; this only reports whether it "
    "separates later behavior. Derived on demand; nothing persisted or executed."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _pct_change(later, base) -> float | None:
    if later is None or base is None or base == 0:
        return None
    return round((later - base) / abs(base) * 100, 4)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return round(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2, 4)


def _rate(flags: list[bool]) -> float | None:
    known = [f for f in flags if f is not None]
    return round(sum(1 for f in known if f) / len(known), 4) if known else None


@dataclass
class ShadowOutcome:
    token_address: str
    review_priority: str
    review_score: float
    structure: float
    velocity: float
    timing: float
    risk_penalty: float
    risk_reasons: list[str]
    top10_pct: float | None
    sniper_pct: float | None
    insider_pct: float | None
    bundler_pct: float | None
    risk_level_start: str | None
    risk_level_end: str | None
    survived: bool | None
    rug_or_liq_removed: bool
    # per-horizon measured changes (percent) + attention persistence
    price_change: dict = field(default_factory=dict)
    liquidity_change: dict = field(default_factory=dict)
    volume_change: dict = field(default_factory=dict)
    attention_persist: dict = field(default_factory=dict)


class MemeShadowService:
    """Reconstructs the review_priority per historical anchor and measures the
    token's later trajectory. Pure read-only analysis."""

    def __init__(
        self,
        diagnostic: MemeMasDiagnosticService | None = None,
        profile: CalibrationProfile = DEFAULT_PROFILE,
    ):
        self.diagnostic = diagnostic or MemeMasDiagnosticService(profile=profile)

    # --- data gathering -----------------------------------------------------

    def _load(self, session: Session, lookback_hours: int):
        start = _now() - timedelta(hours=lookback_hours)
        snaps = session.execute(
            select(MemeAttentionSnapshot)
            .where(MemeAttentionSnapshot.observed_at >= start)
            .order_by(MemeAttentionSnapshot.id)
        ).scalars().all()

        by_token: dict[str, list[MemeAttentionSnapshot]] = {}
        for s in snaps:
            by_token.setdefault(s.token_address, []).append(s)
        for token in by_token:
            by_token[token].sort(key=lambda x: (_aware(x.observed_at), x.id))

        tokens = list(by_token.keys())
        assessments: dict[str, list[CryptoTokenRiskAssessment]] = {}
        catalysts: dict[str, list[datetime]] = {}
        if tokens:
            rows = session.execute(
                select(CryptoTokenRiskAssessment)
                .where(CryptoTokenRiskAssessment.token_address.in_(tokens))
                .order_by(CryptoTokenRiskAssessment.id)
            ).scalars().all()
            for r in rows:
                assessments.setdefault(r.token_address, []).append(r)
            cat_rows = session.execute(
                select(MemeCatalystEvent.subject_ref, MemeCatalystEvent.observed_at)
                .where(MemeCatalystEvent.subject_ref.in_(tokens))
            ).all()
            for ref, at in cat_rows:
                catalysts.setdefault(ref, []).append(_aware(at))
        return by_token, assessments, catalysts

    @staticmethod
    def _assessment_asof(rows: list[CryptoTokenRiskAssessment], when: datetime):
        chosen = None
        for r in rows:
            if _aware(r.created_at) <= when:
                chosen = r
            else:
                break
        return chosen

    # --- per-anchor outcome -------------------------------------------------

    def outcomes(self, session: Session, lookback_hours: int = 48) -> list[ShadowOutcome]:
        by_token, assessments, catalysts = self._load(session, lookback_hours)
        now = _now()
        min_horizon_min = HORIZONS[0][1]
        results: list[ShadowOutcome] = []

        for token, series in by_token.items():
            for i, anchor in enumerate(series):
                anchor_at = _aware(anchor.observed_at)
                # anchor must be old enough to have follow-through, with later data
                if anchor_at > now - timedelta(minutes=min_horizon_min):
                    continue
                later = series[i + 1:]
                if not later:
                    continue

                assessment = self._assessment_asof(assessments.get(token, []), anchor_at)
                cats = [c for c in catalysts.get(token, []) if c <= anchor_at]
                inp = TokenInputs(
                    token_address=token,
                    symbol=anchor.symbol,
                    snapshot=anchor,
                    previous=series[i - 1] if i >= 1 else None,
                    assessment=assessment,
                    catalyst_count=len(cats),
                    snapshot_count=i + 1,
                    source_snapshot_ids=[anchor.id],
                )
                a = self.diagnostic.assess(inp)
                flags = (assessment.flags if assessment else None) or {}

                out = ShadowOutcome(
                    token_address=token,
                    review_priority=a.review_priority,
                    review_score=a.review_score,
                    structure=a.structure_score,
                    velocity=a.velocity_score,
                    timing=a.timing_score,
                    risk_penalty=a.risk_penalty,
                    risk_reasons=a.risk_reasons,
                    top10_pct=flags.get("top10_holder_pct"),
                    sniper_pct=flags.get("sniper_pct"),
                    insider_pct=flags.get("insider_pct"),
                    bundler_pct=flags.get("bundler_pct"),
                    risk_level_start=anchor.risk_level,
                    risk_level_end=later[-1].risk_level,
                    survived=None,
                    rug_or_liq_removed=False,
                )
                self._measure(out, anchor, anchor_at, later)
                results.append(out)
        return results

    def _measure(self, out: ShadowOutcome, anchor, anchor_at, later):
        for label, minutes in HORIZONS:
            target = anchor_at + timedelta(minutes=minutes)
            tol = timedelta(minutes=minutes * HORIZON_TOLERANCE)
            candidates = [s for s in later if abs(_aware(s.observed_at) - target) <= tol]
            if not candidates:
                continue
            nearest = min(candidates, key=lambda s: abs(_aware(s.observed_at) - target))
            pc = _pct_change(nearest.price_usd, anchor.price_usd)
            lc = _pct_change(nearest.liquidity_usd, anchor.liquidity_usd)
            vc = _pct_change(nearest.volume_24h_usd, anchor.volume_24h_usd)
            if pc is not None:
                out.price_change[label] = pc
            if lc is not None:
                out.liquidity_change[label] = lc
            if vc is not None:
                out.volume_change[label] = vc
            if nearest.attention_score is not None and anchor.attention_score is not None:
                out.attention_persist[label] = nearest.attention_score >= anchor.attention_score

        # survival + rug/liquidity-removed from the whole later series
        last = later[-1]
        if last.liquidity_usd is not None and anchor.liquidity_usd:
            out.survived = last.liquidity_usd >= SURVIVAL_LIQUIDITY_FRACTION * anchor.liquidity_usd
        severe_later = any((s.risk_level or "").lower() == "severe" for s in later)
        big_liq_drop = any(
            s.liquidity_usd is not None
            and anchor.liquidity_usd
            and s.liquidity_usd < SURVIVAL_LIQUIDITY_FRACTION * anchor.liquidity_usd
            for s in later
        )
        out.rug_or_liq_removed = bool(severe_later or big_liq_drop)


# --- aggregation + report ---------------------------------------------------


@dataclass
class ShadowCohort:
    name: str
    samples: int
    price_change_mean: dict = field(default_factory=dict)      # horizon -> mean %
    price_change_median: dict = field(default_factory=dict)
    liquidity_change_mean: dict = field(default_factory=dict)
    volume_change_mean: dict = field(default_factory=dict)
    survival_rate: float | None = None
    rug_incidence: float | None = None
    attention_persistence_1h: float | None = None
    label: str = "too_thin"


def _cohort(name: str, group: list[ShadowOutcome]) -> ShadowCohort:
    c = ShadowCohort(name=name, samples=len(group))
    for hlabel, _ in HORIZONS:
        pcs = [o.price_change[hlabel] for o in group if hlabel in o.price_change]
        c.price_change_mean[hlabel] = _mean(pcs)
        c.price_change_median[hlabel] = _median(pcs)
        c.liquidity_change_mean[hlabel] = _mean(
            [o.liquidity_change[hlabel] for o in group if hlabel in o.liquidity_change]
        )
        c.volume_change_mean[hlabel] = _mean(
            [o.volume_change[hlabel] for o in group if hlabel in o.volume_change]
        )
    c.survival_rate = _rate([o.survived for o in group])
    c.rug_incidence = _rate([o.rug_or_liq_removed for o in group])
    c.attention_persistence_1h = _rate([o.attention_persist.get("1h") for o in group])
    c.label = "measured" if len(group) >= MIN_COHORT_SAMPLES else "too_thin"
    return c


def _bucket_score(x: float | None) -> str:
    if x is None:
        return "unknown"
    return "high" if x >= 0.7 else "mid" if x >= 0.4 else "low"


def _bucket_conc(x: float | None, high: float) -> str:
    if x is None:
        return "absent"
    return "high" if x > high else "present"


@dataclass
class MemeShadowReport:
    note: str
    lookback_hours: int
    anchors: int
    horizons: list[str]
    by_review_priority: list[dict] = field(default_factory=list)
    by_review_score_bucket: list[dict] = field(default_factory=list)
    by_risk_penalty_bucket: list[dict] = field(default_factory=list)
    by_risk_reason: list[dict] = field(default_factory=list)
    by_concentration: list[dict] = field(default_factory=list)
    horizon_coverage: dict = field(default_factory=dict)
    calibration_recommendation: str = "too_thin_to_calibrate"


class MemeShadowReportService:
    def __init__(
        self,
        service: MemeShadowService | None = None,
        profile: CalibrationProfile = DEFAULT_PROFILE,
    ):
        self.service = service or MemeShadowService(profile=profile)

    def build(self, session: Session, lookback_hours: int = 48) -> MemeShadowReport:
        outs = self.service.outcomes(session, lookback_hours)

        def cohort_dict(c: ShadowCohort) -> dict:
            return {
                "cohort": c.name,
                "samples": c.samples,
                "label": c.label,
                "price_change_mean": c.price_change_mean,
                "price_change_median": c.price_change_median,
                "liquidity_change_mean": c.liquidity_change_mean,
                "volume_change_mean": c.volume_change_mean,
                "survival_rate": c.survival_rate,
                "rug_incidence": c.rug_incidence,
                "attention_persistence_1h": c.attention_persistence_1h,
            }

        # by review_priority
        by_priority: dict[str, list[ShadowOutcome]] = {p: [] for p in REVIEW_PRIORITIES}
        for o in outs:
            by_priority.setdefault(o.review_priority, []).append(o)
        prio_cohorts = {p: _cohort(p, g) for p, g in by_priority.items() if g}

        # by review_score bucket / risk_penalty bucket
        def bucketed(keyfn) -> list[dict]:
            groups: dict[str, list[ShadowOutcome]] = {}
            for o in outs:
                groups.setdefault(keyfn(o), []).append(o)
            return [cohort_dict(_cohort(k, g)) for k, g in sorted(groups.items())]

        # by risk reason (an anchor can appear under several reasons)
        reason_groups: dict[str, list[ShadowOutcome]] = {}
        for o in outs:
            for r in o.risk_reasons:
                reason_groups.setdefault(r, []).append(o)

        # by concentration bucket (top10 + a combined sniper/insider/bundler flag)
        def conc_key(o: ShadowOutcome) -> str:
            t = _bucket_conc(o.top10_pct, 40.0)
            sib = "flagged" if any(
                v is not None and v > thr
                for v, thr in ((o.sniper_pct, 20), (o.insider_pct, 15), (o.bundler_pct, 25))
            ) else ("present" if any(
                v is not None for v in (o.sniper_pct, o.insider_pct, o.bundler_pct)
            ) else "absent")
            return f"top10:{t}|sib:{sib}"

        report = MemeShadowReport(
            note=NOTE,
            lookback_hours=lookback_hours,
            anchors=len(outs),
            horizons=[h for h, _ in HORIZONS],
            by_review_priority=[cohort_dict(prio_cohorts[p]) for p in REVIEW_PRIORITIES if p in prio_cohorts],
            by_review_score_bucket=bucketed(lambda o: _bucket_score(o.review_score)),
            by_risk_penalty_bucket=bucketed(lambda o: _bucket_score(o.risk_penalty)),
            by_risk_reason=[
                cohort_dict(_cohort(r, g)) for r, g in sorted(reason_groups.items(), key=lambda kv: -len(kv[1]))
            ],
            by_concentration=bucketed(conc_key),
            horizon_coverage={
                h: sum(1 for o in outs if h in o.price_change) for h, _ in HORIZONS
            },
            calibration_recommendation=self._calibration(prio_cohorts),
        )
        return report

    def _calibration(self, prio_cohorts: dict[str, ShadowCohort]) -> str:
        high = prio_cohorts.get("high_review")
        monitor = prio_cohorts.get("monitor")
        if (
            high is None or monitor is None
            or high.label == "too_thin" or monitor.label == "too_thin"
            or high.survival_rate is None or monitor.survival_rate is None
        ):
            return "too_thin_to_calibrate"
        delta = high.survival_rate - monitor.survival_rate
        if delta >= SEPARATION_SURVIVAL_DELTA:
            return "labels_separate_outcomes"
        if delta <= -SEPARATION_SURVIVAL_DELTA:
            return "review_priority_inverted_recheck"
        return "no_material_separation_recalibrate"


# --- MEME-MAS-003: multi-objective calibration metrics ----------------------
# review_priority is a review-attention label, so a single survival yardstick is
# misleading (high-momentum tiers are volatile by nature). These sections score
# the label across SEPARATE objectives, each a read-only MEASUREMENT of market
# movement / survival — never PnL, EV, a return, a fill, or a recommendation.

OBJECTIVES_NOTE = (
    "Read-only multi-objective calibration MEASUREMENT of MEME-MAS review_priority. "
    "Each metric is measured market movement / survival of the token, split by "
    "objective — NOT PnL, NOT EV, NOT a return, NOT a fill, NOT a recommendation, "
    "NOT sizing. `risk_adjusted_movement` is a measured median move discounted by "
    "observed survival (a label-quality diagnostic), never a return or profit."
)


def _positive_rate(group: list[ShadowOutcome], horizon: str) -> float | None:
    vals = [o.price_change[horizon] for o in group if horizon in o.price_change]
    return round(sum(1 for v in vals if v > 0) / len(vals), 4) if vals else None


def _median_price(group: list[ShadowOutcome], horizon: str) -> float | None:
    return _median([o.price_change[horizon] for o in group if horizon in o.price_change])


def _severe_end_rate(group: list[ShadowOutcome]) -> float | None:
    ends = [o.risk_level_end for o in group if o.risk_level_end is not None]
    return round(sum(1 for e in ends if (e or "").lower() == "severe") / len(ends), 4) if ends else None


@dataclass
class MemeShadowObjectives:
    note: str
    profile: str
    lookback_hours: int
    anchors: int
    overall_momentum_positive_rate: float | None
    momentum_followthrough: list[dict] = field(default_factory=list)
    survival_quality: list[dict] = field(default_factory=list)
    risk_adjusted_movement: list[dict] = field(default_factory=list)
    review_queue_efficiency: list[dict] = field(default_factory=list)
    coverage_quality: dict = field(default_factory=dict)


class MemeShadowObjectivesService:
    """Scores review_priority across separate objectives (MEME-MAS-003). Reuses
    the follow-through outcomes; adds no external call and changes no label."""

    def __init__(self, profile: CalibrationProfile = DEFAULT_PROFILE):
        self.service = MemeShadowService(profile=profile)
        self.profile = profile

    def build(self, session: Session, lookback_hours: int = 48) -> MemeShadowObjectives:
        outs = self.service.outcomes(session, lookback_hours)
        total = len(outs)
        overall_pos = _positive_rate(outs, "1h")

        by_priority: dict[str, list[ShadowOutcome]] = {p: [] for p in REVIEW_PRIORITIES}
        for o in outs:
            by_priority.setdefault(o.review_priority, []).append(o)

        momentum, survival, risk_adj, queue = [], [], [], []
        for p in REVIEW_PRIORITIES:
            g = by_priority.get(p, [])
            if not g:
                continue
            pos1h = _positive_rate(g, "1h")
            med1h = _median_price(g, "1h")
            surv = _rate([o.survived for o in g])
            rug = _rate([o.rug_or_liq_removed for o in g])
            momentum.append({
                "priority": p, "n": len(g), "momentum_positive_rate_1h": pos1h,
                "price_1h_median": med1h, "price_6h_median": _median_price(g, "6h"),
                "price_24h_median": _median_price(g, "24h"),
            })
            survival.append({
                "priority": p, "n": len(g), "survival_rate": surv,
                "rug_incidence": rug, "severe_end_rate": _severe_end_rate(g),
            })
            risk_adj.append({
                "priority": p, "n": len(g), "median_price_1h": med1h, "survival_rate": surv,
                "risk_adjusted_1h": round(med1h * surv, 4) if (med1h is not None and surv is not None) else None,
            })
            queue.append({
                "priority": p, "n": len(g),
                "share": round(len(g) / total, 4) if total else None,
                "momentum_positive_rate_1h": pos1h,
                "lift": round(pos1h / overall_pos, 4) if (pos1h is not None and overall_pos) else None,
            })

        # coverage_quality is LABEL-INDEPENDENT (splits by provider coverage, not
        # by review_priority): does missing coverage predict worse outcomes?
        covered = [o for o in outs if "missing_provider_coverage" not in o.risk_reasons]
        missing = [o for o in outs if "missing_provider_coverage" in o.risk_reasons]

        def cov(g: list[ShadowOutcome]) -> dict:
            return {
                "n": len(g), "survival_rate": _rate([o.survived for o in g]),
                "rug_incidence": _rate([o.rug_or_liq_removed for o in g]),
                "momentum_positive_rate_1h": _positive_rate(g, "1h"),
                "price_1h_median": _median_price(g, "1h"),
            }

        return MemeShadowObjectives(
            note=OBJECTIVES_NOTE,
            profile=self.profile.name,
            lookback_hours=lookback_hours,
            anchors=total,
            overall_momentum_positive_rate=overall_pos,
            momentum_followthrough=momentum,
            survival_quality=survival,
            risk_adjusted_movement=risk_adj,
            review_queue_efficiency=queue,
            coverage_quality={"covered": cov(covered), "missing": cov(missing)},
        )
