"""Forecast calibration: score persisted forecasts against observed market
outcomes and aggregate by cohort.

Read-only scoring — Brier, log loss, and absolute error only. No EV, no
sizing, no trade metrics of any kind."""

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ForecastScoreRecord,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
)
from app.schemas import CalibrationSummary, CohortStats
from app.services.outcomes import latest_outcome_for

logger = logging.getLogger(__name__)

LOG_LOSS_EPSILON = 1e-6

STATUS_SCORED = "scored"
STATUS_PENDING = "pending_outcome"
STATUS_UNSCORABLE = "unscorable"

# Prefixes used in score_tags so summaries can group without joins
TAG_FORECASTER = "forecaster:"
TAG_DEPTH = "depth:"
TAG_RISK = "risk:"
TAG_DOMAIN = "domain:"
TAG_FREE = "tag:"


def brier_score(probability: float, outcome: float) -> float:
    return round((probability - outcome) ** 2, 6)


def log_loss(probability: float, outcome: float, epsilon: float = LOG_LOSS_EPSILON) -> float:
    """Binary log loss with epsilon clamp so p=0.0/1.0 stays finite."""
    clamped = min(max(probability, epsilon), 1.0 - epsilon)
    return round(-(outcome * math.log(clamped) + (1.0 - outcome) * math.log(1.0 - clamped)), 6)


def absolute_error(probability: float, outcome: float) -> float:
    return round(abs(probability - outcome), 6)


def _score_target(
    outcome: MarketOutcomeRecord | None,
) -> tuple[str, float | None, str]:
    """(score_status, y in {0.0, 1.0} or None, notes) for an outcome state."""
    if outcome is None:
        return STATUS_PENDING, None, "no outcome synced yet"
    if outcome.outcome_status in ("open", "closed"):
        return STATUS_PENDING, None, f"market is {outcome.outcome_status}, not settled"
    if outcome.outcome_status in ("canceled", "unknown"):
        return STATUS_UNSCORABLE, None, f"outcome is {outcome.outcome_status}"
    # settled
    if outcome.winning_side == "yes":
        return STATUS_SCORED, 1.0, "settled yes"
    if outcome.winning_side == "no":
        return STATUS_SCORED, 0.0, "settled no"
    return STATUS_UNSCORABLE, None, "settled but winning side is void/unknown"


def _score_is_current(
    score: ForecastScoreRecord | None,
    outcome: MarketOutcomeRecord | None,
    forecast: MarketForecastRecord,
) -> bool:
    """Is this persisted score still the right answer for the current outcome?

    Identity of the outcome row is NOT enough. `MarketOutcomeRecord` is upserted
    in place, one row per ticker, so a market that settles yes and is later
    corrected to no keeps the same `id` AND the same `score_status` ("scored").
    Comparing only those two would treat the stale yes-score as current forever,
    and the forecast would be permanently mis-scored against an outcome that no
    longer exists. Recompute and compare.
    """
    if score is None:
        return False
    if score.outcome_id != (outcome.id if outcome else None):
        return False
    target_status, y, _ = _score_target(outcome)
    if score.score_status != target_status:
        return False
    if target_status != STATUS_SCORED or y is None:
        return True
    return score.brier_score == brier_score(forecast.estimated_probability, y)


def _build_score_tags(session: Session, forecast: MarketForecastRecord) -> list[str]:
    tags = [
        f"{TAG_FORECASTER}{forecast.forecaster_name}",
        f"{TAG_DEPTH}{forecast.evidence_depth}",
        f"{TAG_RISK}{forecast.forecast_risk}",
    ]
    if forecast.research_packet_id is not None:
        packet = session.get(MarketResearchPacket, forecast.research_packet_id)
        if packet is not None:
            tags.append(f"{TAG_DOMAIN}{packet.domain}")
    for tag in forecast.calibration_tags or []:
        tags.append(f"{TAG_FREE}{tag}")
    return tags


def latest_score_for(session: Session, forecast_id: int) -> ForecastScoreRecord | None:
    return session.execute(
        select(ForecastScoreRecord)
        .where(ForecastScoreRecord.forecast_id == forecast_id)
        .order_by(ForecastScoreRecord.id.desc())
    ).scalars().first()


class CalibrationService:
    def score_forecast(
        self,
        session: Session,
        forecast: MarketForecastRecord,
        outcome: MarketOutcomeRecord | None,
    ) -> ForecastScoreRecord:
        """Score one forecast against one outcome state and persist the row."""
        status, y, notes = _score_target(outcome)
        row = ForecastScoreRecord(
            forecast_id=forecast.id,
            market_ticker=forecast.market_ticker,
            outcome_id=outcome.id if outcome else None,
            was_resolved=status == STATUS_SCORED,
            score_status=status,
            score_notes=notes,
            score_tags=_build_score_tags(session, forecast),
            created_at=datetime.now(timezone.utc),
        )
        if status == STATUS_SCORED and y is not None:
            probability = forecast.estimated_probability
            row.brier_score = brier_score(probability, y)
            row.log_loss = log_loss(probability, y)
            row.absolute_error = absolute_error(probability, y)
        session.add(row)
        session.commit()
        return row

    def select_scoring_candidates(
        self, session: Session, limit: int
    ) -> list[MarketForecastRecord]:
        """Forecasts whose persisted score does not match their current outcome.

        OUTCOME-SYNC-COVERAGE-001 replaced an id-ordered prefix here. The old
        selection was `order_by(id).limit(limit)`, which loaded the SAME oldest
        `limit` forecasts every cycle and skipped almost all of them as already
        current. Forecasts past the cap were never loaded, so they could never
        be scored — on production that showed up as exactly 1,000 distinct
        scored forecasts spanning forecast ids 1..1000, against 12,543
        forecasts. It was not a backlog draining slowly; it was a prefix.

        The LIMIT now applies to forecasts that actually need work. Bulk-loaded
        (no N+1) and provider-free: this reads persisted state only.
        """
        forecasts = list(session.execute(
            select(MarketForecastRecord).order_by(MarketForecastRecord.id)
        ).scalars().all())
        if not forecasts:
            return []

        tickers = {f.market_ticker for f in forecasts}
        outcomes = {
            o.market_ticker: o
            for o in session.execute(
                select(MarketOutcomeRecord).where(
                    MarketOutcomeRecord.market_ticker.in_(tickers or {""})
                )
            ).scalars()
        }
        # Ascending id -> last write wins == max id, matching latest_score_for.
        latest: dict[int, ForecastScoreRecord] = {}
        for row in session.execute(
            select(ForecastScoreRecord).order_by(ForecastScoreRecord.id)
        ).scalars():
            latest[row.forecast_id] = row

        candidates: list[MarketForecastRecord] = []
        for forecast in forecasts:
            outcome = outcomes.get(forecast.market_ticker)
            existing = latest.get(forecast.id)
            if _score_is_current(existing, outcome, forecast):
                continue  # already current
            candidates.append(forecast)
            if len(candidates) >= limit:
                break
        return candidates

    def score_unscored(self, session: Session, limit: int = 500) -> dict[str, int]:
        """Score forecasts that have no score yet, or whose latest score was
        computed against a different outcome state. Skips forecasts whose
        latest score already matches the current outcome (no duplicates)."""
        counts = {STATUS_SCORED: 0, STATUS_PENDING: 0, STATUS_UNSCORABLE: 0, "skipped": 0}
        for forecast in self.select_scoring_candidates(session, limit):
            outcome = latest_outcome_for(session, forecast.market_ticker)
            existing = latest_score_for(session, forecast.id)
            if _score_is_current(existing, outcome, forecast):
                # Re-checked against live state, not the bulk snapshot: the
                # selection is advisory, this is the guarantee against duplicates.
                counts["skipped"] += 1
                continue
            row = self.score_forecast(session, forecast, outcome)
            counts[row.score_status] += 1
        return counts

    def summary(self, session: Session) -> CalibrationSummary:
        """Aggregate over the LATEST score per forecast (append-only history
        would otherwise double-count re-scored forecasts)."""
        latest_ids = (
            select(func.max(ForecastScoreRecord.id))
            .group_by(ForecastScoreRecord.forecast_id)
            .scalar_subquery()
        )
        rows = session.execute(
            select(ForecastScoreRecord).where(ForecastScoreRecord.id.in_(latest_ids))
        ).scalars().all()

        summary = CalibrationSummary(total_scores=len(rows))
        resolved_rows = []
        for row in rows:
            if row.score_status == STATUS_SCORED:
                resolved_rows.append(row)
            elif row.score_status == STATUS_PENDING:
                summary.pending_outcome += 1
            else:
                summary.unscorable += 1
        summary.resolved = len(resolved_rows)

        def stats_for(group: list[ForecastScoreRecord]) -> CohortStats:
            return CohortStats(
                count=len(group),
                mean_brier=round(sum(r.brier_score for r in group) / len(group), 6),
                mean_log_loss=round(sum(r.log_loss for r in group) / len(group), 6),
                mean_absolute_error=round(sum(r.absolute_error for r in group) / len(group), 6),
            )

        if resolved_rows:
            summary.overall = stats_for(resolved_rows)

        buckets: dict[str, dict[str, list[ForecastScoreRecord]]] = {
            TAG_DEPTH: {},
            TAG_RISK: {},
            TAG_FORECASTER: {},
            TAG_DOMAIN: {},
            TAG_FREE: {},
        }
        for row in resolved_rows:
            for tag in row.score_tags or []:
                for prefix, bucket in buckets.items():
                    if tag.startswith(prefix):
                        bucket.setdefault(tag[len(prefix):], []).append(row)

        summary.by_evidence_depth = {k: stats_for(v) for k, v in buckets[TAG_DEPTH].items()}
        summary.by_forecast_risk = {k: stats_for(v) for k, v in buckets[TAG_RISK].items()}
        summary.by_forecaster = {k: stats_for(v) for k, v in buckets[TAG_FORECASTER].items()}
        summary.by_domain = {k: stats_for(v) for k, v in buckets[TAG_DOMAIN].items()}
        summary.by_tag = {k: stats_for(v) for k, v in buckets[TAG_FREE].items()}
        return summary
