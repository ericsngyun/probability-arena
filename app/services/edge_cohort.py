"""Edge cohort analysis (EDGE-ANALYSIS-001): slices the automated
edge-precheck watchlist / paper_candidate_later population into cohorts and
measures gap follow-through per cohort, so a human can see which market
types and conditions actually show the market moving toward the forecast —
and which should be deprioritized in future gating.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): this is ANALYSIS AND
REPORTING ONLY. Follow-through is market-MOVEMENT analysis (did the midpoint
later move toward the forecast?) — it is NOT PnL, simulates no fills, no
positions, no sizing. No dollar EV, no sides, no recommendations to trade,
no orders, no wallets, no execution. Cohort labels (too_thin / promising /
neutral / weak / exclude_candidate) describe MEASUREMENT quality and how
much more observation a cohort warrants; they authorize nothing and change
no flag, threshold, promotion, forecast, or edge logic. Inputs are existing
DB rows only; this module never calls external APIs.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.edge_precheck import (
    STATUS_NO_GAP,
    STATUS_PAPER_CANDIDATE_LATER,
    STATUS_WATCHLIST,
)

logger = logging.getLogger(__name__)

# Horizons match the frontier-eval follow-through analysis exactly.
FOLLOW_THROUGH_HORIZONS_MINUTES = (5, 15, 30, 60)

# Follow-through is only meaningful for rows that carried a real gap and a
# midpoint (the same population the frontier harness analyzes).
FOLLOW_THROUGH_STATUSES = (STATUS_WATCHLIST, STATUS_PAPER_CANDIDATE_LATER)
VALID_EDGE_STATUSES = (STATUS_WATCHLIST, STATUS_PAPER_CANDIDATE_LATER, STATUS_NO_GAP)

# Cohort labelling thresholds (conservative; deterministic).
MIN_COHORT_FOLLOW_SAMPLES = 12   # below this a cohort is too_thin to read
PROMISING_TOWARD_RATE = 0.55     # samples-weighted mean toward-rate to call promising
WEAK_TOWARD_RATE = 0.45          # below this (but above exclude) => weak
EXCLUDE_TOWARD_RATE = 0.35       # at/below this => exclude_candidate

# MVP-005B paper-design gate (mirrors frontier-eval's MIN_FOLLOW_THROUGH_*):
# design stays blocked unless a cohort clears BOTH a real sample floor and a
# real toward-rate. This module can only ever RECOMMEND observation; it never
# unblocks anything on its own.
MVP_005B_MIN_FOLLOW_SAMPLES = 20
MVP_005B_MIN_TOWARD_RATE = 0.55

LABEL_TOO_THIN = "too_thin"
LABEL_PROMISING = "promising"
LABEL_NEUTRAL = "neutral"
LABEL_WEAK = "weak"
LABEL_EXCLUDE = "exclude_candidate"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _tag_value(tags, prefix: str, default: str = "unknown") -> str:
    for tag in tags or []:
        if tag.startswith(prefix):
            return tag.split(":", 1)[1]
    return default


def _abs_gap_bucket(abs_gap: float | None) -> str:
    if abs_gap is None:
        return "none"
    if abs_gap < 0.05:
        return "<0.05"
    if abs_gap < 0.075:
        return "0.05-0.075"
    if abs_gap < 0.10:
        return "0.075-0.10"
    if abs_gap < 0.15:
        return "0.10-0.15"
    return ">0.15"


def _confidence_bucket(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    if conf < 0.60:
        return "<0.60"
    if conf < 0.65:
        return "0.60"
    return "0.65+"


def _liquidity_bucket(cents: int | None) -> str:
    if cents is None:
        return "unknown"
    if cents < 100_000:
        return "<100k"
    if cents < 1_000_000:
        return "100k-1M"
    if cents < 10_000_000:
        return "1M-10M"
    return ">10M"


def _spread_bucket(cents: int | None) -> str:
    if cents is None:
        return "unknown"
    if cents <= 1:
        return "1"
    if cents <= 2:
        return "2"
    if cents <= 5:
        return "3-5"
    if cents <= 10:
        return "6-10"
    return ">10"


def _game_phase(calibration_tags) -> str:
    tags = calibration_tags or []
    if "late_game" in tags:
        return "late"
    if "mid_game" in tags:
        return "mid"
    if "early_game" in tags:
        return "early"
    return "unknown"


@dataclass
class CohortStats:
    """One cohort within one dimension. Counts + per-horizon follow-through.
    All fields are measurement; none is advice."""

    key: str
    sample: int = 0
    watchlist: int = 0
    paper_candidate_later: int = 0
    invalid: int = 0
    abs_gaps: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    # horizon -> {"samples", "toward", "closures"}
    horizons: dict[str, dict] = field(default_factory=dict)

    def horizon_bucket(self, label: str) -> dict:
        return self.horizons.setdefault(
            label, {"samples": 0, "toward": 0, "closures": []}
        )

    def render(self) -> dict:
        follow: dict[str, dict] = {}
        weighted_rate_num = 0.0
        weighted_rate_den = 0
        max_samples = 0
        for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
            label = f"{minutes}m"
            hb = self.horizons.get(label, {"samples": 0, "toward": 0, "closures": []})
            n = hb["samples"]
            follow[label] = {
                "samples": n,
                "moved_toward_forecast": hb["toward"],
                "moved_toward_rate": _rate(hb["toward"], n),
                "mean_gap_closure_pct": _mean(hb["closures"]),
            }
            if n:
                weighted_rate_num += hb["toward"]
                weighted_rate_den += n
                max_samples = max(max_samples, n)
        blended = round(weighted_rate_num / weighted_rate_den, 4) if weighted_rate_den else None
        label = self._label(max_samples, blended)
        return {
            "key": self.key,
            "sample": self.sample,
            "watchlist": self.watchlist,
            "paper_candidate_later": self.paper_candidate_later,
            "invalid": self.invalid,
            "invalid_rate": _rate(self.invalid, self.sample),
            "mean_abs_gap": _mean(self.abs_gaps),
            "confidence_avg": _mean(self.confidences),
            "follow_through_samples": max_samples,
            "blended_toward_rate": blended,
            "follow_through": follow,
            "recommendation": label,
        }

    def _label(self, follow_samples: int, blended: float | None) -> str:
        if follow_samples < MIN_COHORT_FOLLOW_SAMPLES or blended is None:
            return LABEL_TOO_THIN
        if blended >= PROMISING_TOWARD_RATE:
            return LABEL_PROMISING
        if blended <= EXCLUDE_TOWARD_RATE:
            return LABEL_EXCLUDE
        if blended < WEAK_TOWARD_RATE:
            return LABEL_WEAK
        return LABEL_NEUTRAL


@dataclass
class EdgeCohortReport:
    note: str
    window_hours: int
    total_snapshots: int
    follow_through_rows: int
    dimensions: dict[str, list[dict]]
    overall_follow_through: dict[str, dict]
    observe_more: list[str]
    deprioritize: list[str]
    promising: list[str]
    mvp_005b_blocked: bool
    mvp_005b_reason: str


class EdgeCohortReportService:
    """Builds the cohort analysis. Read-only over persisted rows."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    # --- follow-through (identical methodology to frontier-eval) -------------

    def _follow_samples(self, session: Session, row: EdgePrecheckSnapshot) -> dict:
        """For one snapshot, the follow-through sample per horizon (or absent).
        Market-MOVEMENT only: did the midpoint later move toward the forecast?
        No fills, no positions, no PnL."""
        out: dict[str, dict] = {}
        if (
            row.status not in FOLLOW_THROUGH_STATUSES
            or row.probability_gap is None
            or row.market_midpoint is None
        ):
            return out
        created = _aware(row.created_at)
        for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
            deadline = created + timedelta(minutes=minutes)
            later = session.execute(
                select(MarketPriceTick)
                .where(
                    MarketPriceTick.market_ticker == row.market_ticker,
                    MarketPriceTick.observed_at > created,
                    MarketPriceTick.observed_at <= deadline,
                    MarketPriceTick.midpoint.is_not(None),
                )
                .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
            ).scalars().first()
            if later is None:
                continue
            delta = later.midpoint - row.market_midpoint
            closure = delta / row.probability_gap if row.probability_gap else 0.0
            out[f"{minutes}m"] = {
                "closure_pct": round(closure, 4),
                "moved_toward": bool(closure > 0 and abs(delta) > 1e-9),
            }
        return out

    # --- assembly -----------------------------------------------------------

    def build(self, session: Session, hours: int = 24) -> EdgeCohortReport:
        now = _now()
        start = now - timedelta(hours=hours)
        snapshots = session.execute(
            select(EdgePrecheckSnapshot)
            .where(
                EdgePrecheckSnapshot.created_at >= start,
                EdgePrecheckSnapshot.created_at <= now,
            )
            .order_by(EdgePrecheckSnapshot.id.desc())
        ).scalars().all()

        # Side lookups: signal type (via signal_id) and game phase (via forecast).
        signal_ids = {s.signal_id for s in snapshots if s.signal_id is not None}
        signal_type_by_id: dict[int, str] = {}
        if signal_ids:
            for sid, stype in session.execute(
                select(OpportunitySignal.id, OpportunitySignal.signal_type).where(
                    OpportunitySignal.id.in_(signal_ids)
                )
            ).all():
                signal_type_by_id[sid] = stype or "unknown"

        forecast_ids = {s.forecast_id for s in snapshots if s.forecast_id is not None}
        phase_by_forecast: dict[int, str] = {}
        if forecast_ids:
            for fid, tags in session.execute(
                select(
                    MarketForecastRecord.id, MarketForecastRecord.calibration_tags
                ).where(MarketForecastRecord.id.in_(forecast_ids))
            ).all():
                phase_by_forecast[fid] = _game_phase(tags)

        # Precompute follow-through once per snapshot.
        follow_by_id = {row.id: self._follow_samples(session, row) for row in snapshots}
        follow_rows = sum(1 for f in follow_by_id.values() if f)

        # Dimension name -> (cohort key -> CohortStats)
        dims: dict[str, dict[str, CohortStats]] = {
            "market_type": {},
            "domain": {},
            "gap_sign": {},
            "abs_gap_bucket": {},
            "confidence_bucket": {},
            "signal_type": {},
            "liquidity_bucket": {},
            "spread_bucket": {},
            "game_phase": {},
            "persistence": {},
        }
        overall = CohortStats(key="ALL")

        def keys_for(row: EdgePrecheckSnapshot) -> dict[str, str]:
            gap_sign = (
                "none"
                if row.probability_gap is None
                else ("positive" if row.probability_gap >= 0 else "negative")
            )
            persistence = (
                str(row.persistence_count)
                if (row.persistence_count or 0) < 3
                else "3+"
            )
            return {
                "market_type": _tag_value(row.tags, "market_type:"),
                "domain": _tag_value(row.tags, "domain:"),
                "gap_sign": gap_sign,
                "abs_gap_bucket": _abs_gap_bucket(row.abs_probability_gap),
                "confidence_bucket": _confidence_bucket(row.forecast_confidence),
                "signal_type": signal_type_by_id.get(row.signal_id, "unknown"),
                "liquidity_bucket": _liquidity_bucket(row.liquidity_proxy_cents),
                "spread_bucket": _spread_bucket(row.spread_cents),
                "game_phase": phase_by_forecast.get(row.forecast_id, "unknown"),
                "persistence": persistence,
            }

        for row in snapshots:
            row_keys = keys_for(row)
            cohorts = [overall]
            for dim, key in row_keys.items():
                cohorts.append(dims[dim].setdefault(key, CohortStats(key=key)))
            self._accumulate(row, follow_by_id[row.id], cohorts)

        rendered = {
            dim: sorted(
                (stats.render() for stats in cohorts.values()),
                key=lambda c: -c["sample"],
            )
            for dim, cohorts in dims.items()
        }
        overall_rendered = overall.render()

        observe, deprioritize, promising = self._recommend(rendered)
        blocked, reason = self._mvp_005b_gate(rendered, overall_rendered)

        return EdgeCohortReport(
            note=(
                "Analysis only: cohort gap follow-through is market-MOVEMENT "
                "measurement, not PnL and not advice. Labels describe measurement "
                "quality and how much more observation a cohort warrants; they "
                "authorize no trade, paper trade, EV, sizing, order, or flag change."
            ),
            window_hours=hours,
            total_snapshots=len(snapshots),
            follow_through_rows=follow_rows,
            dimensions=rendered,
            overall_follow_through=overall_rendered["follow_through"],
            observe_more=observe,
            deprioritize=deprioritize,
            promising=promising,
            mvp_005b_blocked=blocked,
            mvp_005b_reason=reason,
        )

    def _accumulate(
        self,
        row: EdgePrecheckSnapshot,
        follow: dict,
        cohorts: list[CohortStats],
    ) -> None:
        is_invalid = row.status not in VALID_EDGE_STATUSES
        for stats in cohorts:
            stats.sample += 1
            if row.status == STATUS_WATCHLIST:
                stats.watchlist += 1
            elif row.status == STATUS_PAPER_CANDIDATE_LATER:
                stats.paper_candidate_later += 1
            if is_invalid:
                stats.invalid += 1
            if row.abs_probability_gap is not None:
                stats.abs_gaps.append(row.abs_probability_gap)
            if row.forecast_confidence is not None:
                stats.confidences.append(row.forecast_confidence)
            for label, sample in follow.items():
                hb = stats.horizon_bucket(label)
                hb["samples"] += 1
                hb["toward"] += 1 if sample["moved_toward"] else 0
                hb["closures"].append(sample["closure_pct"])

    # --- conservative recommendation ---------------------------------------

    def _recommend(self, rendered: dict) -> tuple[list[str], list[str], list[str]]:
        """Turn cohort labels into observation guidance. Never advises trading.
        - observe_more: promising OR neutral cohorts (real signal one way or the
          other needs more data before it means anything).
        - deprioritize: weak / exclude_candidate cohorts with enough samples.
        - promising: cohorts that cleared the promising bar (still just 'watch')."""
        observe: list[str] = []
        deprioritize: list[str] = []
        promising: list[str] = []
        for dim, cohorts in rendered.items():
            for c in cohorts:
                tag = f"{dim}={c['key']}"
                label = c["recommendation"]
                n = c["follow_through_samples"]
                rate = c["blended_toward_rate"]
                detail = f"{tag} (n={n}, toward_rate={rate}, {label})"
                if label == LABEL_PROMISING:
                    promising.append(detail)
                    observe.append(detail)
                elif label == LABEL_NEUTRAL:
                    observe.append(detail)
                elif label in (LABEL_WEAK, LABEL_EXCLUDE):
                    deprioritize.append(detail)
        return observe, deprioritize, promising

    def _mvp_005b_gate(
        self, rendered: dict, overall: dict
    ) -> tuple[bool, str]:
        """MVP-005B-design stays BLOCKED unless the data clearly supports it:
        at least one non-trivial cohort clears both the sample floor and the
        toward-rate bar, AND overall follow-through is not neutral-to-negative.
        This method can only report the gate; it changes nothing."""
        overall_rate = overall["blended_toward_rate"]
        overall_n = overall["follow_through_samples"]
        strong = [
            f"{dim}={c['key']}"
            for dim, cohorts in rendered.items()
            for c in cohorts
            if c["key"] not in ("ALL",)
            and c["follow_through_samples"] >= MVP_005B_MIN_FOLLOW_SAMPLES
            and (c["blended_toward_rate"] or 0) >= MVP_005B_MIN_TOWARD_RATE
        ]
        overall_ok = (
            overall_n >= MVP_005B_MIN_FOLLOW_SAMPLES
            and (overall_rate or 0) >= MVP_005B_MIN_TOWARD_RATE
        )
        if strong and overall_ok:
            return False, (
                "Data-supported cohorts exist "
                f"({', '.join(strong[:5])}) and overall toward-rate "
                f"{overall_rate} >= {MVP_005B_MIN_TOWARD_RATE} over n={overall_n}. "
                "This is a MEASUREMENT signal only — advancing to MVP-005B-design "
                "still requires explicit human acceptance; no capability is unlocked here."
            )
        return True, (
            "BLOCKED: no cohort clears both the sample floor "
            f"(n>={MVP_005B_MIN_FOLLOW_SAMPLES}) and toward-rate "
            f">={MVP_005B_MIN_TOWARD_RATE}"
            + (
                f"; overall toward-rate {overall_rate} over n={overall_n}"
                if overall_rate is not None
                else "; no follow-through samples yet"
            )
            + ". Keep collecting; do not start MVP-005B-design."
        )
