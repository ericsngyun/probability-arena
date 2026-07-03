"""Signal promotion and signal-triggered intelligence refresh.

Workflow: the watcher emits informational signals (status 'new'). Promotion
marks selected signals 'promoted_to_research' using deterministic priority
rules. Processing then refreshes the market's intelligence — fresh detail
enrichment, resolution assessment, research packet, and forecast — links the
refreshed rows to the signal, and marks it 'forecast_refreshed'.

Conservative by design: whichever judge/collector/forecaster the existing env
flags select is used (template everything unless ENABLE_* flags are true).
This is workflow plumbing, not alpha. Nothing here computes EV, sizes
positions, paper trades, recommends trades, or places orders —
'paper_candidate_pending' is a human review label only.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, OpportunitySignal
from app.schemas import SignalReport, RefreshedSignalSummary

logger = logging.getLogger(__name__)

# Order matters: promote-top picks in this priority, then newest first.
PROMOTION_PRIORITY = (
    "price_move_threshold",
    "price_crossed_latest_forecast",
    "spread_tightened",
    "liquidity_appeared",
    "newly_two_sided",
)

STATUS_NEW = "new"
STATUS_REVIEWED = "reviewed"
STATUS_DISMISSED = "dismissed"
STATUS_PROMOTED = "promoted_to_research"
STATUS_RESEARCH_REFRESHED = "research_refreshed"
STATUS_FORECAST_REFRESHED = "forecast_refreshed"
STATUS_PAPER_CANDIDATE_PENDING = "paper_candidate_pending"  # review label; no paper trading exists

ALL_STATUSES = (
    STATUS_NEW,
    STATUS_REVIEWED,
    STATUS_DISMISSED,
    STATUS_PROMOTED,
    STATUS_RESEARCH_REFRESHED,
    STATUS_FORECAST_REFRESHED,
    STATUS_PAPER_CANDIDATE_PENDING,
)


class SignalNotFoundError(LookupError):
    pass


class PromotionNotAllowedError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _priority_index(signal_type: str) -> int:
    try:
        return PROMOTION_PRIORITY.index(signal_type)
    except ValueError:
        return len(PROMOTION_PRIORITY)


class SignalPromotionService:
    def list_recent(
        self,
        session: Session,
        limit: int = 20,
        signal_status: str | None = None,
    ) -> list[OpportunitySignal]:
        """Recent signals, newest first (optionally filtered by status)."""
        query = select(OpportunitySignal).order_by(OpportunitySignal.id.desc()).limit(limit)
        if signal_status:
            query = query.where(OpportunitySignal.signal_status == signal_status)
        return list(session.execute(query).scalars().all())

    def promote(self, session: Session, signal_id: int) -> OpportunitySignal:
        """Promote one 'new' signal. Idempotent for already-promoted signals;
        dismissed/reviewed/processed signals cannot be promoted."""
        signal = session.get(OpportunitySignal, signal_id)
        if signal is None:
            raise SignalNotFoundError(f"Signal {signal_id} not found")
        if signal.signal_status == STATUS_PROMOTED:
            return signal  # duplicate promotion is a no-op
        if signal.signal_status != STATUS_NEW:
            raise PromotionNotAllowedError(
                f"Signal {signal_id} has status {signal.signal_status!r}; "
                "only 'new' signals can be promoted"
            )
        signal.signal_status = STATUS_PROMOTED
        signal.promoted_at = _now()
        session.commit()
        return signal

    def promote_top(self, session: Session, limit: int = 5) -> list[OpportunitySignal]:
        """Promote up to `limit` 'new' signals by deterministic priority:
        signal type per PROMOTION_PRIORITY, then newest first — at most one
        signal per market ticker per batch."""
        candidates = list(
            session.execute(
                select(OpportunitySignal).where(OpportunitySignal.signal_status == STATUS_NEW)
            ).scalars().all()
        )
        candidates.sort(key=lambda s: (_priority_index(s.signal_type), -s.id))

        promoted: list[OpportunitySignal] = []
        seen_tickers: set[str] = set()
        for signal in candidates:
            if len(promoted) >= limit:
                break
            if signal.market_ticker in seen_tickers:
                continue
            seen_tickers.add(signal.market_ticker)
            promoted.append(self.promote(session, signal.id))
        return promoted


class SignalProcessingService:
    """Refreshes intelligence for promoted signals. All collaborators are
    injectable; defaults follow the env flags (template-only unless the
    ENABLE_LLM_*/ENABLE_EXTERNAL_RESEARCH flags are true)."""

    def __init__(self, enrichment_adapter=None, judge=None, collector=None, forecaster=None):
        self.enrichment_adapter = enrichment_adapter
        self.judge = judge
        self.collector = collector
        self.forecaster = forecaster

    async def process(self, session: Session, signal: OpportunitySignal) -> OpportunitySignal:
        """Fresh enrichment -> resolution assessment -> research packet ->
        forecast for the signal's market, linking refreshed rows to the
        signal. On failure the error is recorded on the signal and the status
        stays at the last completed stage (auditable partial state)."""
        from app.schemas import MarketData
        from app.services.enrichment import MarketDetailEnrichmentService, apply_latest_enrichment
        from app.services.forecasting import ForecastingService
        from app.services.research import create_research_packet, get_collector
        from app.services.resolution import get_judge, persist_assessment

        try:
            market = session.execute(
                select(Market).where(Market.ticker == signal.market_ticker)
            ).scalar_one_or_none()
            if market is None:
                raise LookupError(
                    f"Market {signal.market_ticker!r} has no stored metadata; run a scan first"
                )

            # 1. Fresh detail enrichment (latest market detail + event + series)
            enrichment_service = MarketDetailEnrichmentService(adapter=self.enrichment_adapter)
            await enrichment_service.enrich_ticker(session, market.ticker, scanner_run_id=None)

            # 2. Fresh resolution assessment on the enriched view
            judge = self.judge or get_judge()
            market_data = apply_latest_enrichment(
                session,
                MarketData(
                    ticker=market.ticker,
                    event_ticker=market.event_ticker,
                    title=market.title or "",
                    category=market.category,
                    status=market.status,
                    close_time=market.close_time,
                    expiration_time=market.expiration_time,
                    rules_primary=market.rules_primary,
                ),
            )
            assessment = await judge.assess(market_data)
            persist_assessment(session, market.ticker, assessment, judge, scanner_run_id=None)

            # 3. Fresh research packet
            collector = self.collector or get_collector()
            packet = await create_research_packet(
                session, market, collector=collector, scanner_run_id=None
            )
            signal.refreshed_research_packet_id = packet.id
            signal.signal_status = STATUS_RESEARCH_REFRESHED
            session.commit()

            # 4. Fresh forecast (consumes the packet just created)
            forecast_row = await ForecastingService(forecaster=self.forecaster).forecast_market(
                session, market, scanner_run_id=None
            )
            signal.refreshed_forecast_id = forecast_row.id
            signal.signal_status = STATUS_FORECAST_REFRESHED
            signal.processed_at = _now()
            session.commit()
        except Exception as exc:
            session.rollback()
            logger.exception("Signal %s processing failed", signal.id)
            signal.processing_error_type = type(exc).__name__
            signal.processing_error_message = str(exc)[:2000]
            signal.processed_at = _now()
            session.commit()
        return signal

    async def process_promoted(self, session: Session, limit: int = 5) -> list[OpportunitySignal]:
        """Process up to `limit` promoted signals, oldest promotion first.
        Signals that previously failed (processing_error_type set) are skipped
        until the error is cleared manually."""
        signals = session.execute(
            select(OpportunitySignal)
            .where(
                OpportunitySignal.signal_status == STATUS_PROMOTED,
                OpportunitySignal.processing_error_type.is_(None),
            )
            .order_by(OpportunitySignal.promoted_at.asc(), OpportunitySignal.id.asc())
            .limit(limit)
        ).scalars().all()
        return [await self.process(session, signal) for signal in signals]


def build_signal_report(session: Session, recent_limit: int = 10) -> SignalReport:
    """Aggregate view of the signal workflow. Informational only."""
    from sqlalchemy import func

    from app.models import MarketForecastRecord

    by_status = dict(
        session.execute(
            select(OpportunitySignal.signal_status, func.count()).group_by(
                OpportunitySignal.signal_status
            )
        ).all()
    )
    by_type = dict(
        session.execute(
            select(OpportunitySignal.signal_type, func.count()).group_by(
                OpportunitySignal.signal_type
            )
        ).all()
    )
    awaiting = session.execute(
        select(func.count()).select_from(OpportunitySignal).where(
            OpportunitySignal.signal_status == STATUS_PROMOTED,
            OpportunitySignal.processing_error_type.is_(None),
        )
    ).scalar() or 0
    errored = session.execute(
        select(func.count()).select_from(OpportunitySignal).where(
            OpportunitySignal.processing_error_type.is_not(None)
        )
    ).scalar() or 0

    refreshed_rows = session.execute(
        select(OpportunitySignal, MarketForecastRecord)
        .join(
            MarketForecastRecord,
            OpportunitySignal.refreshed_forecast_id == MarketForecastRecord.id,
        )
        .where(OpportunitySignal.signal_status == STATUS_FORECAST_REFRESHED)
        .order_by(OpportunitySignal.processed_at.desc(), OpportunitySignal.id.desc())
        .limit(recent_limit)
    ).all()
    recent_refreshed = [
        RefreshedSignalSummary(
            signal_id=signal.id,
            market_ticker=signal.market_ticker,
            signal_type=signal.signal_type,
            refreshed_forecast_id=forecast.id,
            refreshed_probability=forecast.estimated_probability,
            refreshed_confidence=forecast.confidence,
            processed_at=signal.processed_at,
        )
        for signal, forecast in refreshed_rows
    ]

    return SignalReport(
        total=sum(by_status.values()),
        by_status=by_status,
        by_type=by_type,
        promoted_awaiting_processing=awaiting,
        processed_with_errors=errored,
        recent_refreshed=recent_refreshed,
    )
