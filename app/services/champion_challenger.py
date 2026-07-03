"""Champion/challenger forecaster comparison (MVP-004G).

Answers "does the challenger forecaster beat the baseline?" from the existing
append-only calibration data (forecast_scores + market_forecasts + optional
signal linkage). Deliberately NOT persisted: every comparison is deterministic
and reproducible from source tables, so a stored comparison row would only be
a cache that can drift; the doc for this decision lives in the README's
champion/challenger section.

Method:
- Take the LATEST score per forecast (append-only re-scoring), then the latest
  SCORED forecast per (forecaster, ticker) as that side's representative.
- Aggregate (unpaired) metrics over representatives per side.
- Pair tickers where both sides have representatives scored against the SAME
  outcome; paired deltas and per-market win rate are the stronger evidence.
- Cohort tables (market type, signal type, confidence bucket, evidence depth,
  risk, domain, game stage) are unpaired and labeled as such.
- Sample-size labels gate interpretation; a warning is attached whenever the
  smaller side is below the minimum count.

Read-only measurement. No EV, no trade semantics of any kind — this report is
the gate that must be passed BEFORE MVP-005A (EV precheck) is even designed.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ForecastScoreRecord, MarketForecastRecord, OpportunitySignal
from app.schemas import (
    ComparisonMetric,
    ForecasterCohortComparison,
    ForecasterComparisonSummary,
    ForecasterPairComparison,
    ForecasterSideSummary,
)

logger = logging.getLogger(__name__)

DEFAULT_BASELINE = "template_baseline"
DEFAULT_CHALLENGER = "baseball_evidence_v1"

CONFIDENCE_BUCKETS = (
    (0.00, 0.25, "0.00-0.25"),
    (0.25, 0.50, "0.25-0.50"),
    (0.50, 0.60, "0.50-0.60"),
    (0.60, 0.70, "0.60-0.70"),
    (0.70, 0.80, "0.70-0.80"),
    (0.80, 1.01, "0.80-1.00"),
)

LABEL_INSUFFICIENT = "insufficient_sample"  # n < 30
LABEL_EARLY = "early_signal"  # 30 <= n < 100
LABEL_USEFUL = "useful_sample"  # 100 <= n < 300
LABEL_STRONGER = "stronger_sample"  # n >= 300


def sample_label(n: int) -> str:
    if n < 30:
        return LABEL_INSUFFICIENT
    if n < 100:
        return LABEL_EARLY
    if n < 300:
        return LABEL_USEFUL
    return LABEL_STRONGER


def confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    for low, high, label in CONFIDENCE_BUCKETS:
        if low <= confidence < high:
            return label
    return "unknown"


def _forecaster_matches(forecast: MarketForecastRecord, param: str) -> bool:
    """Match by bare name or name_version composite (baseball_evidence_v1)."""
    return (
        forecast.forecaster_name == param
        or f"{forecast.forecaster_name}_{forecast.forecaster_version}" == param
    )


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass
class _Entry:
    forecast: MarketForecastRecord
    score: ForecastScoreRecord
    side: str  # "baseline" | "challenger"
    signal_type: str | None

    @property
    def scored(self) -> bool:
        return self.score.score_status == "scored"

    @property
    def ticker(self) -> str:
        return self.forecast.market_ticker

    def tag_dim(self, prefix: str, default: str) -> str:
        for tag in self.forecast.calibration_tags or []:
            if tag.startswith(prefix):
                return tag[len(prefix):]
        return default

    @property
    def market_type(self) -> str:
        return self.tag_dim("market_type_", "unknown")

    @property
    def game_stage(self) -> str:
        tags = self.forecast.calibration_tags or []
        if "late_game" in tags:
            return "late_game"
        if "early_game" in tags:
            return "early_game"
        return "unknown"

    @property
    def domain(self) -> str:
        for tag in self.score.score_tags or []:
            if tag.startswith("domain:"):
                return tag[len("domain:"):]
        return "unknown"

    @property
    def confidence_bucket(self) -> str:
        return confidence_bucket(self.forecast.confidence)


def _metric(entries: list[_Entry]) -> ComparisonMetric:
    scored = [e for e in entries if e.scored]
    if not scored:
        return ComparisonMetric(count_scored=0)
    return ComparisonMetric(
        count_scored=len(scored),
        mean_brier=round(sum(e.score.brier_score for e in scored) / len(scored), 6),
        mean_log_loss=round(sum(e.score.log_loss for e in scored) / len(scored), 6),
        mean_absolute_error=round(
            sum(e.score.absolute_error for e in scored) / len(scored), 6
        ),
    )


def _delta(challenger: float | None, baseline: float | None) -> float | None:
    if challenger is None or baseline is None:
        return None
    return round(challenger - baseline, 6)


class ChampionChallengerService:
    def _load_entries(
        self, session: Session, baseline: str, challenger: str
    ) -> list[_Entry]:
        latest_score_ids = (
            select(func.max(ForecastScoreRecord.id))
            .group_by(ForecastScoreRecord.forecast_id)
            .scalar_subquery()
        )
        rows = session.execute(
            select(ForecastScoreRecord, MarketForecastRecord)
            .join(
                MarketForecastRecord,
                ForecastScoreRecord.forecast_id == MarketForecastRecord.id,
            )
            .where(ForecastScoreRecord.id.in_(latest_score_ids))
        ).all()

        forecast_ids = [forecast.id for _, forecast in rows]
        signal_types: dict[int, str] = {}
        if forecast_ids:
            for forecast_id, signal_type in session.execute(
                select(OpportunitySignal.refreshed_forecast_id, OpportunitySignal.signal_type)
                .where(OpportunitySignal.refreshed_forecast_id.in_(forecast_ids))
                .order_by(OpportunitySignal.id)
            ).all():
                signal_types[forecast_id] = signal_type  # latest signal wins

        entries: list[_Entry] = []
        for score, forecast in rows:
            if _forecaster_matches(forecast, baseline):
                side = "baseline"
            elif _forecaster_matches(forecast, challenger):
                side = "challenger"
            else:
                continue
            entries.append(
                _Entry(
                    forecast=forecast,
                    score=score,
                    side=side,
                    signal_type=signal_types.get(forecast.id),
                )
            )
        return entries

    @staticmethod
    def _apply_filters(
        entries: list[_Entry],
        domain: str | None,
        market_type: str | None,
        signal_type: str | None,
        min_created_at: datetime | None,
        max_created_at: datetime | None,
    ) -> list[_Entry]:
        result = entries
        if domain:
            result = [e for e in result if e.domain == domain]
        if market_type:
            result = [e for e in result if e.market_type == market_type]
        if signal_type:
            result = [e for e in result if e.signal_type == signal_type]
        if min_created_at:
            floor = _aware(min_created_at)
            result = [e for e in result if _aware(e.forecast.created_at) >= floor]
        if max_created_at:
            ceiling = _aware(max_created_at)
            result = [e for e in result if _aware(e.forecast.created_at) <= ceiling]
        return result

    @staticmethod
    def _representatives(entries: list[_Entry]) -> dict[str, _Entry]:
        """Latest SCORED forecast per ticker for one side."""
        reps: dict[str, _Entry] = {}
        for entry in entries:
            if not entry.scored:
                continue
            current = reps.get(entry.ticker)
            if current is None or entry.forecast.id > current.forecast.id:
                reps[entry.ticker] = entry
        return reps

    def _cohort_rows(
        self,
        base_reps: list[_Entry],
        chal_reps: list[_Entry],
        dim_fn,
    ) -> list[ForecasterCohortComparison]:
        cohorts: dict[str, dict[str, list[_Entry]]] = {}
        for side, reps in (("baseline", base_reps), ("challenger", chal_reps)):
            for entry in reps:
                cohorts.setdefault(dim_fn(entry), {}).setdefault(side, []).append(entry)
        rows = []
        for name in sorted(cohorts):
            base_metric = _metric(cohorts[name].get("baseline", []))
            chal_metric = _metric(cohorts[name].get("challenger", []))
            rows.append(
                ForecasterCohortComparison(
                    cohort=name,
                    baseline=base_metric,
                    challenger=chal_metric,
                    delta_brier=_delta(chal_metric.mean_brier, base_metric.mean_brier),
                    delta_log_loss=_delta(chal_metric.mean_log_loss, base_metric.mean_log_loss),
                    delta_absolute_error=_delta(
                        chal_metric.mean_absolute_error, base_metric.mean_absolute_error
                    ),
                    sample_label=sample_label(
                        min(base_metric.count_scored, chal_metric.count_scored)
                    ),
                    paired=False,
                )
            )
        return rows

    def compare(
        self,
        session: Session,
        baseline: str = DEFAULT_BASELINE,
        challenger: str = DEFAULT_CHALLENGER,
        domain: str | None = None,
        market_type: str | None = None,
        signal_type: str | None = None,
        min_created_at: datetime | None = None,
        max_created_at: datetime | None = None,
        paired_only: bool = False,
        min_count: int = 30,
    ) -> ForecasterComparisonSummary:
        entries = self._load_entries(session, baseline, challenger)
        entries = self._apply_filters(
            entries, domain, market_type, signal_type, min_created_at, max_created_at
        )
        base_entries = [e for e in entries if e.side == "baseline"]
        chal_entries = [e for e in entries if e.side == "challenger"]

        base_reps_map = self._representatives(base_entries)
        chal_reps_map = self._representatives(chal_entries)

        # Paired: same ticker, same outcome
        pairs: list[tuple[_Entry, _Entry]] = []
        for ticker, base_rep in base_reps_map.items():
            chal_rep = chal_reps_map.get(ticker)
            if (
                chal_rep is not None
                and base_rep.score.outcome_id is not None
                and base_rep.score.outcome_id == chal_rep.score.outcome_id
            ):
                pairs.append((base_rep, chal_rep))

        paired_comparison: ForecasterPairComparison | None = None
        if pairs:
            deltas_brier = [c.score.brier_score - b.score.brier_score for b, c in pairs]
            deltas_ll = [c.score.log_loss - b.score.log_loss for b, c in pairs]
            deltas_ae = [c.score.absolute_error - b.score.absolute_error for b, c in pairs]
            wins = sum(1 for d in deltas_brier if d < -1e-9)
            losses = sum(1 for d in deltas_brier if d > 1e-9)
            ties = len(pairs) - wins - losses
            paired_comparison = ForecasterPairComparison(
                pair_count=len(pairs),
                wins=wins,
                losses=losses,
                ties=ties,
                win_rate_by_market=round(wins / len(pairs), 4),
                mean_delta_brier=round(sum(deltas_brier) / len(pairs), 6),
                mean_delta_log_loss=round(sum(deltas_ll) / len(pairs), 6),
                mean_delta_absolute_error=round(sum(deltas_ae) / len(pairs), 6),
                sample_label=sample_label(len(pairs)),
            )

        if paired_only:
            paired_tickers = {b.ticker for b, _ in pairs}
            base_reps = [e for t, e in base_reps_map.items() if t in paired_tickers]
            chal_reps = [e for t, e in chal_reps_map.items() if t in paired_tickers]
            basis = "paired"
        else:
            base_reps = list(base_reps_map.values())
            chal_reps = list(chal_reps_map.values())
            basis = "unpaired"

        base_metric = _metric(base_reps)
        chal_metric = _metric(chal_reps)

        def side_summary(name: str, reps_metric: ComparisonMetric, all_entries: list[_Entry]):
            pending = sum(1 for e in all_entries if e.score.score_status == "pending_outcome")
            unscorable = sum(1 for e in all_entries if e.score.score_status == "unscorable")
            return ForecasterSideSummary(
                forecaster=name,
                scored=reps_metric,
                coverage=len(all_entries),
                pending=pending,
                unscorable=unscorable,
            )

        min_n = min(base_metric.count_scored, chal_metric.count_scored)
        warning = None
        if min_n < min_count:
            warning = (
                f"insufficient sample (baseline scored n={base_metric.count_scored}, "
                f"challenger scored n={chal_metric.count_scored}; threshold {min_count}) — "
                "do NOT infer edge from this report yet"
            )

        filters = {
            key: value
            for key, value in (
                ("domain", domain),
                ("market_type", market_type),
                ("signal_type", signal_type),
                ("min_created_at", min_created_at.isoformat() if min_created_at else None),
                ("max_created_at", max_created_at.isoformat() if max_created_at else None),
                ("paired_only", paired_only or None),
            )
            if value is not None
        }

        return ForecasterComparisonSummary(
            baseline_forecaster=baseline,
            challenger_forecaster=challenger,
            filters=filters,
            comparison_basis=basis,
            baseline=side_summary(baseline, base_metric, base_entries),
            challenger=side_summary(challenger, chal_metric, chal_entries),
            delta_brier=_delta(chal_metric.mean_brier, base_metric.mean_brier),
            delta_log_loss=_delta(chal_metric.mean_log_loss, base_metric.mean_log_loss),
            delta_absolute_error=_delta(
                chal_metric.mean_absolute_error, base_metric.mean_absolute_error
            ),
            paired=paired_comparison,
            sample_label=sample_label(min_n),
            warning=warning,
            by_market_type=self._cohort_rows(base_reps, chal_reps, lambda e: e.market_type),
            by_signal_type=self._cohort_rows(
                [e for e in base_reps if e.signal_type],
                [e for e in chal_reps if e.signal_type],
                lambda e: e.signal_type,
            ),
            by_confidence_bucket=self._cohort_rows(
                base_reps, chal_reps, lambda e: e.confidence_bucket
            ),
            by_evidence_depth=self._cohort_rows(
                base_reps, chal_reps, lambda e: e.forecast.evidence_depth
            ),
            by_forecast_risk=self._cohort_rows(
                base_reps, chal_reps, lambda e: e.forecast.forecast_risk
            ),
            by_domain=self._cohort_rows(base_reps, chal_reps, lambda e: e.domain),
            by_game_stage=self._cohort_rows(base_reps, chal_reps, lambda e: e.game_stage),
        )
