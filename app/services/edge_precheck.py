"""Edge precheck (MVP-005A): probability-gap MEASUREMENT.

Records `probability_gap = forecast.estimated_probability - market_midpoint`
for recent forecasts, together with validity checks (resolution, evidence
depth, freshness, confidence, spread, liquidity) and a persistence count.
Everything lands as append-only edge_precheck_snapshots rows a human reads.

Hard boundary (docs/MVP_005A_EDGE_PRECHECK_DESIGN.md, SAFETY_BOUNDARIES):
this layer is measurement, not advice. No dollar EV, no sides, no
directions, no sizes, no orders, no wallets, no execution — and no
downstream behavior may branch on gap sign or size. 'paper_candidate_later'
is a review label for a possible future, separately-gated MVP-005B; it
carries zero behavior. Inputs are existing DB rows only; this module never
calls external APIs.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)

logger = logging.getLogger(__name__)

STATUS_NO_GAP = "no_gap"
STATUS_WATCHLIST = "watchlist"
STATUS_INVALID_WIDE_SPREAD = "invalid_wide_spread"
STATUS_INVALID_LOW_LIQUIDITY = "invalid_low_liquidity"
STATUS_INVALID_LOW_CONFIDENCE = "invalid_low_confidence"
STATUS_INVALID_STALE_FORECAST = "invalid_stale_forecast"
STATUS_INVALID_STALE_SNAPSHOT = "invalid_stale_market_snapshot"
STATUS_INVALID_RESOLUTION = "invalid_resolution_risk"
STATUS_INVALID_NOT_SOURCE_BACKED = "invalid_not_source_backed"
STATUS_PAPER_CANDIDATE_LATER = "paper_candidate_later"  # review label; NO behavior

# Deterministic precedence: the first failing check names the status;
# every failing check is still collected in invalidation_reasons.
INVALID_PRECEDENCE = (
    STATUS_INVALID_RESOLUTION,
    STATUS_INVALID_NOT_SOURCE_BACKED,
    STATUS_INVALID_STALE_FORECAST,
    STATUS_INVALID_STALE_SNAPSHOT,
    STATUS_INVALID_LOW_CONFIDENCE,
    STATUS_INVALID_WIDE_SPREAD,
    STATUS_INVALID_LOW_LIQUIDITY,
)

ALL_STATUSES = (
    STATUS_NO_GAP,
    STATUS_WATCHLIST,
    *INVALID_PRECEDENCE,
    STATUS_PAPER_CANDIDATE_LATER,
)

SPORTS_DOMAINS = ("sports_baseball", "sports_soccer", "sports_tennis")

# Series-name fragments -> market type tag (measurement cohorts only)
MARKET_TYPE_MARKERS = (
    ("total", ("TOTAL", "GOALS")),
    ("spread", ("SPREAD", "HANDICAP", "HCAP")),
    ("winner", ("GAME", "MATCH", "WIN")),
)


@dataclass
class EdgePrecheckConfig:
    min_abs_gap: float = 0.05
    max_spread_cents: int = 10
    min_liquidity_cents: int = 500
    min_confidence: float = 0.60
    max_forecast_age_seconds: int = 900
    max_live_sports_forecast_age_seconds: int = 300
    max_market_snapshot_age_seconds: int = 120
    require_source_backed: bool = True
    require_researchable: bool = True
    required_persistence_snapshots: int = 3

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "EdgePrecheckConfig":
        s = settings or get_settings()
        return cls(
            min_abs_gap=s.edge_precheck_min_abs_gap,
            max_spread_cents=s.edge_precheck_max_spread_cents,
            min_liquidity_cents=s.edge_precheck_min_liquidity_cents,
            min_confidence=s.edge_precheck_min_confidence,
            max_forecast_age_seconds=s.edge_precheck_max_forecast_age_seconds,
            max_live_sports_forecast_age_seconds=(
                s.edge_precheck_max_live_sports_forecast_age_seconds
            ),
            max_market_snapshot_age_seconds=s.edge_precheck_max_market_snapshot_age_seconds,
            require_source_backed=s.edge_precheck_require_source_backed,
            require_researchable=s.edge_precheck_require_researchable,
            required_persistence_snapshots=s.edge_precheck_required_persistence_snapshots,
        )

    def as_dict(self) -> dict:
        from dataclasses import asdict

        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _domain_for(ticker: str) -> str:
    from app.services.research import DOMAIN_GENERAL, DOMAIN_RULES

    upper = ticker.upper()
    for domain, markers, _keywords in DOMAIN_RULES:
        if any(upper.startswith(marker) for marker in markers):
            return domain
    return DOMAIN_GENERAL


def _market_type_for(ticker: str) -> str:
    series = ticker.upper().split("-", 1)[0]
    for market_type, markers in MARKET_TYPE_MARKERS:
        if any(marker in series for marker in markers):
            return market_type
    return "unknown"


class EdgePrecheckService:
    """Creates measurement snapshots for forecasts. Reads existing rows
    only — never calls external APIs, never recommends anything."""

    def __init__(self, config: EdgePrecheckConfig | None = None):
        self.config = config or EdgePrecheckConfig.from_settings()

    # --- input helpers ------------------------------------------------------

    def _latest_tick(self, session: Session, ticker: str) -> MarketPriceTick | None:
        return session.execute(
            select(MarketPriceTick)
            .where(MarketPriceTick.market_ticker == ticker)
            .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
        ).scalars().first()

    def _linked_signal_id(self, session: Session, forecast_id: int) -> int | None:
        return session.execute(
            select(OpportunitySignal.id)
            .where(OpportunitySignal.refreshed_forecast_id == forecast_id)
            .order_by(OpportunitySignal.id.desc())
        ).scalars().first()

    def _persistence_count(
        self,
        session: Session,
        ticker: str,
        forecaster_name: str,
        gap_sign: int,
    ) -> int:
        """1 + the streak of immediately-prior snapshots for the same
        (ticker, forecaster) that were watchlist/candidate with the same gap
        direction. Any other row breaks the streak."""
        prior = session.execute(
            select(EdgePrecheckSnapshot)
            .where(
                EdgePrecheckSnapshot.market_ticker == ticker,
                EdgePrecheckSnapshot.forecaster_name == forecaster_name,
            )
            .order_by(EdgePrecheckSnapshot.id.desc())
            .limit(20)
        ).scalars().all()
        streak = 1
        for row in prior:
            if (
                row.status in (STATUS_WATCHLIST, STATUS_PAPER_CANDIDATE_LATER)
                and row.probability_gap is not None
                and (1 if row.probability_gap >= 0 else -1) == gap_sign
            ):
                streak += 1
            else:
                break
        return streak

    # --- the measurement ------------------------------------------------------

    def precheck_forecast(
        self, session: Session, forecast: MarketForecastRecord, now: datetime | None = None
    ) -> EdgePrecheckSnapshot:
        """One measurement snapshot for one forecast (persisted)."""
        from app.services.resolution import latest_assessment_for

        cfg = self.config
        now = now or _now()
        ticker = forecast.market_ticker
        domain = _domain_for(ticker)

        tick = self._latest_tick(session, ticker)
        resolution = latest_assessment_for(session, ticker)

        forecast_age = int((now - (_aware(forecast.created_at) or now)).total_seconds())
        snapshot_age = (
            int((now - (_aware(tick.observed_at) or now)).total_seconds())
            if tick is not None
            else None
        )
        midpoint = tick.midpoint if tick is not None else None
        gap = (
            round(forecast.estimated_probability - midpoint, 4)
            if midpoint is not None
            else None
        )

        max_forecast_age = (
            cfg.max_live_sports_forecast_age_seconds
            if domain in SPORTS_DOMAINS
            else cfg.max_forecast_age_seconds
        )

        # Collect ALL failing checks in precedence order (design §5)
        reasons: list[str] = []
        if cfg.require_researchable and (
            resolution is None or resolution.tradeability != "researchable"
        ):
            reasons.append(STATUS_INVALID_RESOLUTION)
        if cfg.require_source_backed and forecast.evidence_depth != "source_backed":
            reasons.append(STATUS_INVALID_NOT_SOURCE_BACKED)
        if forecast_age > max_forecast_age:
            reasons.append(STATUS_INVALID_STALE_FORECAST)
        if tick is None or snapshot_age is None or (
            snapshot_age > cfg.max_market_snapshot_age_seconds
        ):
            reasons.append(STATUS_INVALID_STALE_SNAPSHOT)
        if forecast.confidence < cfg.min_confidence:
            reasons.append(STATUS_INVALID_LOW_CONFIDENCE)
        if tick is None or tick.spread is None or tick.spread > cfg.max_spread_cents:
            reasons.append(STATUS_INVALID_WIDE_SPREAD)
        if (
            tick is None
            or tick.liquidity_proxy is None
            or tick.liquidity_proxy < cfg.min_liquidity_cents
        ):
            reasons.append(STATUS_INVALID_LOW_LIQUIDITY)

        persistence = 1
        if reasons:
            status = reasons[0]  # deterministic precedence
        elif gap is None or abs(gap) < cfg.min_abs_gap:
            status = STATUS_NO_GAP
        else:
            persistence = self._persistence_count(
                session, ticker, forecast.forecaster_name, 1 if gap >= 0 else -1
            )
            status = (
                STATUS_PAPER_CANDIDATE_LATER  # review label only; no behavior attaches
                if persistence >= cfg.required_persistence_snapshots
                else STATUS_WATCHLIST
            )

        row = EdgePrecheckSnapshot(
            market_ticker=ticker,
            signal_id=self._linked_signal_id(session, forecast.id),
            forecast_id=forecast.id,
            market_snapshot_id=tick.id if tick is not None else None,
            resolution_assessment_id=resolution.id if resolution is not None else None,
            forecaster_name=forecast.forecaster_name,
            evidence_depth=forecast.evidence_depth,
            forecast_probability=forecast.estimated_probability,
            forecast_confidence=forecast.confidence,
            forecast_risk=forecast.forecast_risk,
            market_midpoint=midpoint,
            yes_bid=tick.yes_bid if tick is not None else None,
            yes_ask=tick.yes_ask if tick is not None else None,
            spread_cents=tick.spread if tick is not None else None,
            liquidity_proxy_cents=tick.liquidity_proxy if tick is not None else None,
            probability_gap=gap,
            abs_probability_gap=round(abs(gap), 4) if gap is not None else None,
            status=status,
            invalidation_reasons=reasons,
            forecast_age_seconds=forecast_age,
            market_snapshot_age_seconds=snapshot_age,
            persistence_count=persistence,
            thresholds=self.config.as_dict(),
            tags=[f"domain:{domain}", f"market_type:{_market_type_for(ticker)}"],
            raw_context={
                "measurement_only": True,
                "forecast_created_at": (
                    _aware(forecast.created_at).isoformat() if forecast.created_at else None
                ),
                "tick_observed_at": (
                    _aware(tick.observed_at).isoformat()
                    if tick is not None and tick.observed_at
                    else None
                ),
                "max_forecast_age_applied": max_forecast_age,
            },
            created_at=now,
        )
        session.add(row)
        session.flush()
        return row

    def run_batch(
        self, session: Session, limit: int = 50, now: datetime | None = None
    ) -> list[EdgePrecheckSnapshot]:
        """Measure the latest forecast per ticker across the most recent
        `limit` forecasts. Persists one snapshot per forecast evaluated."""
        recent = session.execute(
            select(MarketForecastRecord)
            .order_by(MarketForecastRecord.id.desc())
            .limit(limit * 3)  # headroom so per-ticker dedup still fills `limit`
        ).scalars().all()
        latest_per_ticker: dict[str, MarketForecastRecord] = {}
        for forecast in recent:
            latest_per_ticker.setdefault(forecast.market_ticker, forecast)
        selected = list(latest_per_ticker.values())[:limit]
        snapshots = [self.precheck_forecast(session, forecast, now=now) for forecast in selected]
        session.commit()
        return snapshots


class EdgePrecheckReportService:
    """Aggregate view of the measurement rows. Every output is
    measurement-only — no advice fields exist."""

    def build(self, session: Session, recent_limit: int = 10):
        from app.schemas import EdgePrecheckReport, EdgePrecheckSnapshotOut

        rows = session.execute(
            select(EdgePrecheckSnapshot).order_by(EdgePrecheckSnapshot.id.desc()).limit(1000)
        ).scalars().all()

        by_status: dict[str, int] = {}
        by_forecaster: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        by_market_type: dict[str, int] = {}
        reason_counts: dict[str, int] = {}
        gaps: list[float] = []
        for row in rows:
            by_status[row.status] = by_status.get(row.status, 0) + 1
            by_forecaster[row.forecaster_name] = by_forecaster.get(row.forecaster_name, 0) + 1
            for tag in row.tags or []:
                if tag.startswith("domain:"):
                    key = tag.split(":", 1)[1]
                    by_domain[key] = by_domain.get(key, 0) + 1
                elif tag.startswith("market_type:"):
                    key = tag.split(":", 1)[1]
                    by_market_type[key] = by_market_type.get(key, 0) + 1
            for reason in row.invalidation_reasons or []:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if row.probability_gap is not None:
                gaps.append(row.probability_gap)

        largest = sorted(
            (row for row in rows if row.abs_probability_gap is not None),
            key=lambda row: -row.abs_probability_gap,
        )[:recent_limit]

        total = session.execute(
            select(func.count()).select_from(EdgePrecheckSnapshot)
        ).scalar() or 0

        return EdgePrecheckReport(
            note=(
                "Measurement only: probability gaps between forecasts and market "
                "midpoints. Not dollar EV, not advice; paper_candidate_later is a "
                "review label with no attached behavior."
            ),
            total_snapshots=total,
            by_status=by_status,
            by_forecaster=by_forecaster,
            by_domain=by_domain,
            by_market_type=by_market_type,
            mean_gap=round(sum(gaps) / len(gaps), 4) if gaps else None,
            mean_abs_gap=(
                round(sum(abs(g) for g in gaps) / len(gaps), 4) if gaps else None
            ),
            paper_candidate_later_count=by_status.get(STATUS_PAPER_CANDIDATE_LATER, 0),
            invalidation_reason_counts=dict(
                sorted(reason_counts.items(), key=lambda item: -item[1])
            ),
            recent_largest_gaps=[
                EdgePrecheckSnapshotOut.model_validate(row) for row in largest
            ],
        )
