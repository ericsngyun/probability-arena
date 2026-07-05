"""Edge shadow-policy analysis (EDGE-POLICY-001): simulates candidate cohort
FILTERS over already-recorded edge-precheck watchlist / paper_candidate_later
rows, to ask a single measurement question — would excluding weak cohorts
leave a stronger measurement population? — and, where outcomes exist, whether
settlement Brier agrees with short-horizon follow-through.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md, MVP-005A design §1):
this is READ-ONLY SHADOW ANALYSIS. It changes no live gate, threshold,
promotion, forecaster, edge-precheck, flag, or service — it only re-slices
rows that already exist. Follow-through is market-MOVEMENT measurement (not
PnL, no fills, no positions). Settlement analysis is forecast-vs-market
calibration on resolved outcomes (Brier / log-loss) — NOT dollar EV, NOT
PnL, NOT a trade. Policy labels describe measurement quality only; they
authorize no trade, paper trade, EV, sizing, order, or capital, and starting
MVP-005B still requires separate explicit human acceptance. Inputs are
existing DB rows only; this module never calls external APIs.
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import EdgePrecheckSnapshot, MarketForecastRecord, MarketOutcomeRecord
from app.services.edge_cohort import (
    FOLLOW_THROUGH_HORIZONS_MINUTES,
    FOLLOW_THROUGH_STATUSES,
    MVP_005B_MIN_FOLLOW_SAMPLES,
    MVP_005B_MIN_TOWARD_RATE,
    VALID_EDGE_STATUSES,
    EdgeCohortReportService,
    _abs_gap_bucket,
    _aware,
    _confidence_bucket,
    _game_phase,
    _liquidity_bucket,
    _mean,
    _rate,
    _spread_bucket,
    _tag_value,
)

logger = logging.getLogger(__name__)

# Policy label ladder (measurement quality only; authorizes nothing).
POLICY_TOO_THIN = "too_thin"
POLICY_WORSE = "worse_than_baseline"
POLICY_NEUTRAL = "neutral"
POLICY_PROMISING = "promising_shadow"
POLICY_REJECT = "reject_policy"

MIN_POLICY_FOLLOW_SAMPLES = 12       # below this a policy is unreadable
POLICY_IMPROVEMENT_EPSILON = 0.03    # blended-rate delta vs baseline to matter
POLICY_PROMISING_RATE = MVP_005B_MIN_TOWARD_RATE   # 0.55 at 30m or 60m
POLICY_PROMISING_MIN_SAMPLES = MVP_005B_MIN_FOLLOW_SAMPLES  # 20
POLICY_REJECT_SAMPLE_FRACTION = 0.4  # keeps <40% of baseline samples AND worse
POLICY_REJECT_RATE = 0.35            # 30m rate at/below this while worse

_LOG_EPS = 1e-6


def _derive(row: EdgePrecheckSnapshot) -> dict:
    """Read-only derived attributes used by policy predicates."""
    gap = row.probability_gap
    return {
        "status": row.status,
        "is_valid": row.status in VALID_EDGE_STATUSES,
        "is_ft": row.status in FOLLOW_THROUGH_STATUSES,
        "market_type": _tag_value(row.tags, "market_type:"),
        "domain": _tag_value(row.tags, "domain:"),
        "game_phase": None,  # filled from forecast lookup by caller
        "abs_gap": row.abs_probability_gap,
        "abs_gap_bucket": _abs_gap_bucket(row.abs_probability_gap),
        "confidence": row.forecast_confidence,
        "conf_bucket": _confidence_bucket(row.forecast_confidence),
        "liquidity": row.liquidity_proxy_cents,
        "liq_bucket": _liquidity_bucket(row.liquidity_proxy_cents),
        "spread": row.spread_cents,
        "spread_bucket": _spread_bucket(row.spread_cents),
        "persistence": row.persistence_count or 0,
        "gap_sign": ("none" if gap is None else ("positive" if gap >= 0 else "negative")),
    }


# --- policy predicates (keep-if-True), over a derived-attrs dict -------------

def _keep_all(a: dict) -> bool:
    return True


def _exclude_winner(a: dict) -> bool:
    return a["market_type"] != "winner"


def _exclude_late(a: dict) -> bool:
    return a["game_phase"] != "late"


def _exclude_conf_065(a: dict) -> bool:
    return a["conf_bucket"] != "0.65+"


def _exclude_absgap_gt_015(a: dict) -> bool:
    return a["abs_gap"] is not None and a["abs_gap"] <= 0.15


def _exclude_liq_1m_10m(a: dict) -> bool:
    return a["liq_bucket"] != "1M-10M"


def _small_gap_only(a: dict) -> bool:
    return a["abs_gap_bucket"] in ("0.05-0.075", "0.075-0.10")


def _spread_2_5c_only(a: dict) -> bool:
    return a["spread_bucket"] in ("2", "3-5")


def _liq_lt_100k_only(a: dict) -> bool:
    return a["liq_bucket"] == "<100k"


def _totals_only(a: dict) -> bool:
    return a["market_type"] == "total"


def _spreads_only(a: dict) -> bool:
    return a["market_type"] == "spread"


def _exclude_all_bad(a: dict) -> bool:
    # every current exclude/deprioritize candidate at once
    return (
        _exclude_winner(a)
        and _exclude_late(a)
        and _exclude_conf_065(a)
        and _exclude_absgap_gt_015(a)
        and _exclude_liq_1m_10m(a)
        and a["persistence"] != 2
    )


def _conservative_candidate(a: dict) -> bool:
    return (
        _exclude_winner(a)
        and _exclude_late(a)
        and _exclude_absgap_gt_015(a)
        and _exclude_liq_1m_10m(a)
        and a["market_type"] in ("spread", "total")
        and _small_gap_only(a)
        and (_spread_2_5c_only(a) or _liq_lt_100k_only(a))
    )


# name -> predicate; order preserved in the report (baseline first).
POLICIES: tuple[tuple[str, object], ...] = (
    ("baseline_all_watchlist", _keep_all),
    ("exclude_winner", _exclude_winner),
    ("exclude_late_game", _exclude_late),
    ("exclude_confidence_065_plus", _exclude_conf_065),
    ("exclude_abs_gap_gt_015", _exclude_absgap_gt_015),
    ("exclude_liquidity_1m_10m", _exclude_liq_1m_10m),
    ("small_gap_only_005_010", _small_gap_only),
    ("spread_2_5c_only", _spread_2_5c_only),
    ("liquidity_lt_100k_only", _liq_lt_100k_only),
    ("totals_only", _totals_only),
    ("spreads_only", _spreads_only),
    ("exclude_all_current_bad_cohorts", _exclude_all_bad),
    ("conservative_candidate_policy", _conservative_candidate),
)


@dataclass
class PolicyResult:
    name: str
    included: int = 0          # kept snapshots (all statuses matching predicate)
    watchlist: int = 0
    paper_candidate_later: int = 0
    invalid: int = 0
    no_gap: int = 0
    follow_samples: int = 0
    blended_toward_rate: float | None = None
    horizons: dict[str, dict] = field(default_factory=dict)
    market_type_dist: dict[str, int] = field(default_factory=dict)
    domain_dist: dict[str, int] = field(default_factory=dict)
    gap_bucket_dist: dict[str, int] = field(default_factory=dict)
    confidence_dist: dict[str, int] = field(default_factory=dict)
    persistence_dist: dict[str, int] = field(default_factory=dict)
    settlement: dict = field(default_factory=dict)
    recommendation: str = POLICY_TOO_THIN

    def render(self) -> dict:
        return {
            "name": self.name,
            "included": self.included,
            "watchlist": self.watchlist,
            "paper_candidate_later": self.paper_candidate_later,
            "invalid": self.invalid,
            "no_gap": self.no_gap,
            "invalid_rate": _rate(self.invalid, self.included),
            "follow_samples": self.follow_samples,
            "blended_toward_rate": self.blended_toward_rate,
            "follow_through": self.horizons,
            "market_type_dist": self.market_type_dist,
            "domain_dist": self.domain_dist,
            "gap_bucket_dist": self.gap_bucket_dist,
            "confidence_dist": self.confidence_dist,
            "persistence_dist": self.persistence_dist,
            "settlement": self.settlement,
            "recommendation": self.recommendation,
        }


@dataclass
class EdgePolicyReport:
    note: str
    window_hours: int
    population: int
    policies: list[dict]
    any_clears_follow_gate: list[str]
    any_improves_over_baseline: list[str]
    any_preserves_sample: list[str]
    settlement_available: bool
    settlement_disagreement: str
    mvp_005b_blocked: bool
    mvp_005b_reason: str


class EdgePolicyReportService:
    """Builds the shadow-policy analysis. Read-only over persisted rows."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._cohort = EdgeCohortReportService(self.settings)  # reuse follow-through

    def build(self, session: Session, hours: int = 24) -> EdgePolicyReport:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        snapshots = session.execute(
            select(EdgePrecheckSnapshot)
            .where(
                EdgePrecheckSnapshot.created_at >= start,
                EdgePrecheckSnapshot.created_at <= now,
            )
            .order_by(EdgePrecheckSnapshot.id.desc())
        ).scalars().all()

        # Game phase from the linked forecast's calibration tags.
        forecast_ids = {s.forecast_id for s in snapshots if s.forecast_id is not None}
        phase_by_forecast: dict[int, str] = {}
        if forecast_ids:
            for fid, tags in session.execute(
                select(
                    MarketForecastRecord.id, MarketForecastRecord.calibration_tags
                ).where(MarketForecastRecord.id.in_(forecast_ids))
            ).all():
                phase_by_forecast[fid] = _game_phase(tags)

        # Settled binary outcomes for the tickers we hold (settlement analysis).
        tickers = {s.market_ticker for s in snapshots}
        outcome_by_ticker: dict[str, float] = {}
        if tickers:
            for tkr, status, resolved in session.execute(
                select(
                    MarketOutcomeRecord.market_ticker,
                    MarketOutcomeRecord.outcome_status,
                    MarketOutcomeRecord.resolved_probability,
                ).where(MarketOutcomeRecord.market_ticker.in_(tickers))
            ).all():
                if status == "settled" and resolved in (0.0, 1.0):
                    outcome_by_ticker[tkr] = resolved

        # Precompute derived attrs + follow-through once per snapshot.
        derived: dict[int, dict] = {}
        follow: dict[int, dict] = {}
        for row in snapshots:
            a = _derive(row)
            a["game_phase"] = phase_by_forecast.get(row.forecast_id, "unknown")
            derived[row.id] = a
            follow[row.id] = self._cohort._follow_samples(session, row)

        baseline = None
        results: list[PolicyResult] = []
        for name, predicate in POLICIES:
            kept = [r for r in snapshots if predicate(derived[r.id])]
            res = self._aggregate(name, kept, derived, follow, outcome_by_ticker)
            if name == "baseline_all_watchlist":
                baseline = res
            results.append(res)

        for res in results:
            res.recommendation = self._label(res, baseline)

        rendered = [r.render() for r in results]
        decision = self._decide(results, baseline)
        return EdgePolicyReport(
            note=(
                "Read-only shadow analysis: simulates cohort FILTERS over existing "
                "watchlist/paper_candidate_later rows. Changes no live gate, flag, or "
                "logic. Follow-through is market-movement (not PnL); settlement is "
                "forecast-vs-market Brier on resolved outcomes (not EV, not PnL, not a "
                "trade). Labels authorize nothing; MVP-005B still needs explicit acceptance."
            ),
            window_hours=hours,
            population=sum(1 for r in snapshots if derived[r.id]["is_ft"]),
            policies=rendered,
            settlement_available=bool(outcome_by_ticker),
            **decision,
        )

    def _aggregate(
        self,
        name: str,
        kept: list[EdgePrecheckSnapshot],
        derived: dict[int, dict],
        follow: dict[int, dict],
        outcome_by_ticker: dict[str, float],
    ) -> PolicyResult:
        res = PolicyResult(name=name)
        horizon_acc = {
            f"{m}m": {"samples": 0, "toward": 0, "closures": []}
            for m in FOLLOW_THROUGH_HORIZONS_MINUTES
        }
        # settlement accumulators
        fc_brier: list[float] = []
        mkt_brier: list[float] = []
        fc_ll: list[float] = []
        beats = 0
        resolved_n = 0

        for row in kept:
            a = derived[row.id]
            res.included += 1
            if a["status"] == "watchlist":
                res.watchlist += 1
            elif a["status"] == "paper_candidate_later":
                res.paper_candidate_later += 1
            elif a["status"] == "no_gap":
                res.no_gap += 1
            if not a["is_valid"]:
                res.invalid += 1

            if a["is_ft"]:
                res.market_type_dist[a["market_type"]] = (
                    res.market_type_dist.get(a["market_type"], 0) + 1
                )
                res.domain_dist[a["domain"]] = res.domain_dist.get(a["domain"], 0) + 1
                res.gap_bucket_dist[a["abs_gap_bucket"]] = (
                    res.gap_bucket_dist.get(a["abs_gap_bucket"], 0) + 1
                )
                res.confidence_dist[a["conf_bucket"]] = (
                    res.confidence_dist.get(a["conf_bucket"], 0) + 1
                )
                pkey = str(a["persistence"]) if a["persistence"] < 3 else "3+"
                res.persistence_dist[pkey] = res.persistence_dist.get(pkey, 0) + 1

                for label, sample in follow[row.id].items():
                    hb = horizon_acc[label]
                    hb["samples"] += 1
                    hb["toward"] += 1 if sample["moved_toward"] else 0
                    hb["closures"].append(sample["closure_pct"])

                # settlement (resolved binary outcomes only)
                y = outcome_by_ticker.get(row.market_ticker)
                if (
                    y is not None
                    and row.forecast_probability is not None
                    and row.market_midpoint is not None
                ):
                    resolved_n += 1
                    fp = row.forecast_probability
                    mid = row.market_midpoint
                    fb = (fp - y) ** 2
                    mb = (mid - y) ** 2
                    fc_brier.append(fb)
                    mkt_brier.append(mb)
                    p = min(max(fp, _LOG_EPS), 1 - _LOG_EPS)
                    fc_ll.append(-(y * math.log(p) + (1 - y) * math.log(1 - p)))
                    if fb < mb:
                        beats += 1

        # follow-through render + blended rate
        num = den = 0
        max_samples = 0
        for m in FOLLOW_THROUGH_HORIZONS_MINUTES:
            label = f"{m}m"
            hb = horizon_acc[label]
            n = hb["samples"]
            res.horizons[label] = {
                "samples": n,
                "moved_toward_rate": _rate(hb["toward"], n),
                "mean_gap_closure_pct": _mean(hb["closures"]),
            }
            if n:
                num += hb["toward"]
                den += n
                max_samples = max(max_samples, n)
        res.follow_samples = max_samples
        res.blended_toward_rate = round(num / den, 4) if den else None

        res.settlement = {
            "resolved_samples": resolved_n,
            "forecast_brier": _mean(fc_brier),
            "market_midpoint_brier": _mean(mkt_brier),
            "forecast_minus_market_brier": (
                round(_mean(fc_brier) - _mean(mkt_brier), 4)
                if fc_brier and mkt_brier
                else None
            ),
            "forecast_log_loss": _mean(fc_ll),
            "forecast_beats_market_rate": _rate(beats, resolved_n),
        }
        return res

    def _label(self, res: PolicyResult, baseline: PolicyResult | None) -> str:
        n = res.follow_samples
        rate = res.blended_toward_rate
        if n < MIN_POLICY_FOLLOW_SAMPLES or rate is None:
            return POLICY_TOO_THIN
        r30 = res.horizons.get("30m", {}).get("moved_toward_rate") or 0
        r60 = res.horizons.get("60m", {}).get("moved_toward_rate") or 0
        base_rate = (baseline.blended_toward_rate or 0) if baseline else 0
        base_n = baseline.follow_samples if baseline else 0

        if (
            n >= POLICY_PROMISING_MIN_SAMPLES
            and (r30 >= POLICY_PROMISING_RATE or r60 >= POLICY_PROMISING_RATE)
            and rate >= base_rate + POLICY_IMPROVEMENT_EPSILON
        ):
            return POLICY_PROMISING
        if rate <= base_rate - POLICY_IMPROVEMENT_EPSILON:
            starves = base_n and n < POLICY_REJECT_SAMPLE_FRACTION * base_n
            if starves or r30 <= POLICY_REJECT_RATE:
                return POLICY_REJECT
            return POLICY_WORSE
        return POLICY_NEUTRAL

    def _decide(self, results: list[PolicyResult], baseline: PolicyResult | None) -> dict:
        base_rate = (baseline.blended_toward_rate or 0) if baseline else 0
        clears, improves, preserves = [], [], []
        for r in results:
            if r.name == "baseline_all_watchlist":
                continue
            r30 = r.horizons.get("30m", {}).get("moved_toward_rate") or 0
            r60 = r.horizons.get("60m", {}).get("moved_toward_rate") or 0
            if r.follow_samples >= POLICY_PROMISING_MIN_SAMPLES and (
                r30 >= POLICY_PROMISING_RATE or r60 >= POLICY_PROMISING_RATE
            ):
                clears.append(f"{r.name} (n={r.follow_samples}, 30m={r30}, 60m={r60})")
            if (r.blended_toward_rate or 0) >= base_rate + POLICY_IMPROVEMENT_EPSILON:
                improves.append(
                    f"{r.name} (blended={r.blended_toward_rate} vs baseline {base_rate})"
                )
            if r.follow_samples >= POLICY_PROMISING_MIN_SAMPLES:
                preserves.append(f"{r.name} (n={r.follow_samples})")

        # settlement vs short-horizon follow-through disagreement
        disagreement = self._settlement_disagreement(results)

        promising = [r for r in results if r.recommendation == POLICY_PROMISING]
        if promising:
            blocked = False
            reason = (
                "A shadow policy cleared the follow-through gate "
                f"({', '.join(p.name for p in promising)}). This is a MEASUREMENT signal "
                "on a re-slice of existing rows only — it does NOT change live gating and "
                "advancing to MVP-005B-design still requires explicit human acceptance; no "
                "capability is unlocked here."
            )
        else:
            blocked = True
            reason = (
                "BLOCKED: no shadow policy clears n>="
                f"{POLICY_PROMISING_MIN_SAMPLES} with moved-toward >="
                f"{POLICY_PROMISING_RATE} at 30m or 60m while improving over baseline "
                f"({base_rate}). Filters re-slice the same weak population; keep "
                "collecting. Do not start MVP-005B-design."
            )
        return {
            "any_clears_follow_gate": clears,
            "any_improves_over_baseline": improves,
            "any_preserves_sample": preserves,
            "settlement_disagreement": disagreement,
            "mvp_005b_blocked": blocked,
            "mvp_005b_reason": reason,
        }

    def _settlement_disagreement(self, results: list[PolicyResult]) -> str:
        """Does settlement Brier favor the forecast even where short-horizon
        follow-through looks weak? Reported, never acted on."""
        flags = []
        for r in results:
            s = r.settlement
            n = s.get("resolved_samples", 0)
            delta = s.get("forecast_minus_market_brier")
            if n >= MIN_POLICY_FOLLOW_SAMPLES and delta is not None:
                weak_follow = (r.blended_toward_rate or 0) < POLICY_PROMISING_RATE
                forecast_better = delta < 0
                if weak_follow and forecast_better:
                    flags.append(
                        f"{r.name}: follow-through weak ({r.blended_toward_rate}) but "
                        f"forecast Brier beats market by {-delta} over n={n} resolved"
                    )
        if not flags:
            return (
                "No resolved-outcome disagreement detectable "
                "(insufficient settled markets in window, or settlement agrees with "
                "follow-through). Settlement analysis is calibration only, not EV/PnL."
            )
        return " | ".join(flags)
