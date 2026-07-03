"""Baseline pipeline runner: executes the complete read-only measurement loop
(scan -> enrich -> assess -> research -> forecast -> sync outcomes -> score ->
calibration report) and records parent/stage audit rows.

Overlap protection: a pipeline_runs row with status='running' acts as the
lock; a second invocation exits gracefully as 'skipped'. Rows older than
STALE_LOCK_SECONDS are treated as crashed leftovers and ignored.

Read-only throughout — this runner accumulates baseline calibration data and
adds no EV, sizing, paper-trading, or execution behavior.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Market, MarketSnapshot, PipelineRun, PipelineStageRun, ScannerRun
from app.schemas import MarketData
from app.services.calibration import CalibrationService
from app.services.enrichment import (
    EnrichmentError,
    MarketDetailEnrichmentService,
    apply_latest_enrichment,
)
from app.services.forecasting import ForecastingService, MissingResearchPacketError
from app.services.outcomes import OutcomeService
from app.services.research import create_research_packet, get_collector, latest_packet_for
from app.services.resolution import get_judge, persist_assessment
from app.services.scanner import run_scan

logger = logging.getLogger(__name__)

RUN_TYPE_BASELINE = "baseline"
STALE_LOCK_SECONDS = 6 * 3600

STAGE_SCAN = "scan"
STAGE_ENRICH = "enrich_details"
STAGE_ASSESS = "assess_resolution"
STAGE_RESEARCH = "collect_research"
STAGE_FORECAST = "forecast"
STAGE_SYNC_OUTCOMES = "sync_outcomes"
STAGE_SCORE = "score_forecasts"
STAGE_REPORT = "calibration_report"
STAGE_RETENTION = "retention"  # optional; appended when run_retention is on

BASELINE_STAGES = (
    STAGE_SCAN,
    STAGE_ENRICH,
    STAGE_ASSESS,
    STAGE_RESEARCH,
    STAGE_FORECAST,
    STAGE_SYNC_OUTCOMES,
    STAGE_SCORE,
    STAGE_REPORT,
)


@dataclass
class BaselineConfig:
    scan_limit: int = 500
    candidate_limit: int = 20
    sync_outcome_limit: int = 200
    score_limit: int = 1000
    fail_fast: bool = False
    dry_run: bool = False
    run_retention: bool = False  # append a retention stage at the end

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides) -> "BaselineConfig":
        settings = settings or get_settings()
        values = {
            "scan_limit": settings.baseline_scan_limit,
            "candidate_limit": settings.baseline_candidate_limit,
            "sync_outcome_limit": settings.baseline_sync_outcome_limit,
            "score_limit": settings.baseline_score_limit,
            "fail_fast": settings.baseline_fail_fast,
            "run_retention": settings.enable_pipeline_retention,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)


@dataclass
class StageResult:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    summary: dict | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    # SQLite round-trips datetimes as naive; normalize before subtracting
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=timezone.utc)
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _top_candidates(session: Session, limit: int) -> tuple[ScannerRun, list]:
    run = session.execute(
        select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
    ).scalars().first()
    if run is None:
        raise RuntimeError("no successful scan available for candidate stages")
    rows = session.execute(
        select(MarketSnapshot, Market)
        .join(Market, MarketSnapshot.market_id == Market.id)
        .where(MarketSnapshot.scanner_run_id == run.id, MarketSnapshot.score > 0)
        .order_by(MarketSnapshot.score.desc())
        .limit(limit)
    ).all()
    return run, rows


class PipelineRunner:
    """All collaborators are injectable so tests never touch the network."""

    def __init__(
        self,
        scan_adapter=None,
        enrichment_adapter=None,
        outcome_adapter=None,
        collector=None,
        forecaster=None,
        judge=None,
    ):
        self.scan_adapter = scan_adapter
        self.enrichment_adapter = enrichment_adapter
        self.outcome_adapter = outcome_adapter
        self.collector = collector
        self.forecaster = forecaster
        self.judge = judge

    # --- stages -----------------------------------------------------------

    async def _stage_scan(self, session: Session, cfg: BaselineConfig) -> StageResult:
        result = await run_scan(
            session, adapter=self.scan_adapter, max_markets=cfg.scan_limit, source="pipeline"
        )
        return StageResult(
            attempted=result.run.markets_fetched,
            succeeded=result.run.markets_fetched,
            summary={
                "scanner_run_id": result.run.id,
                "eligible": len(result.ranked),
                "rejected": len(result.rejected),
            },
        )

    async def _stage_enrich(self, session: Session, cfg: BaselineConfig) -> StageResult:
        run, rows = _top_candidates(session, cfg.candidate_limit)
        service = MarketDetailEnrichmentService(adapter=self.enrichment_adapter)
        result = StageResult(attempted=len(rows))
        for _, market in rows:
            try:
                await service.enrich_ticker(session, market.ticker, scanner_run_id=run.id)
                result.succeeded += 1
            except EnrichmentError:
                result.failed += 1
        result.summary = {"scanner_run_id": run.id}
        return result

    async def _stage_assess(self, session: Session, cfg: BaselineConfig) -> StageResult:
        run, rows = _top_candidates(session, cfg.candidate_limit)
        judge = self.judge or get_judge()
        result = StageResult(attempted=len(rows))
        for snapshot, market in rows:
            market_data = apply_latest_enrichment(
                session,
                MarketData(
                    ticker=market.ticker,
                    title=market.title or "",
                    status=market.status,
                    close_time=market.close_time,
                    rules_primary=market.rules_primary,
                    yes_bid=snapshot.yes_bid,
                    yes_ask=snapshot.yes_ask,
                ),
            )
            assessment = await judge.assess(market_data)
            persist_assessment(session, market.ticker, assessment, judge, scanner_run_id=run.id)
            result.succeeded += 1
        result.summary = {"judge": judge.model_name}
        return result

    async def _stage_research(self, session: Session, cfg: BaselineConfig) -> StageResult:
        run, rows = _top_candidates(session, cfg.candidate_limit)
        collector = self.collector or get_collector()
        result = StageResult(attempted=len(rows))
        domains: dict[str, int] = {}
        for _, market in rows:
            packet = await create_research_packet(
                session, market, collector=collector, scanner_run_id=run.id
            )
            domains[packet.domain] = domains.get(packet.domain, 0) + 1
            result.succeeded += 1
        result.summary = {"collector": collector.name, "domains": domains}
        return result

    async def _stage_forecast(self, session: Session, cfg: BaselineConfig) -> StageResult:
        run, rows = _top_candidates(session, cfg.candidate_limit)
        service = ForecastingService(forecaster=self.forecaster)
        result = StageResult(attempted=len(rows))
        skipped_no_packet = 0
        for _, market in rows:
            if latest_packet_for(session, market.ticker) is None:
                skipped_no_packet += 1
                continue
            try:
                await service.forecast_market(session, market, scanner_run_id=run.id)
                result.succeeded += 1
            except MissingResearchPacketError:
                skipped_no_packet += 1
        result.summary = {
            "forecaster": service.forecaster.name,
            "skipped_no_packet": skipped_no_packet,
        }
        return result

    async def _stage_sync_outcomes(self, session: Session, cfg: BaselineConfig) -> StageResult:
        service = OutcomeService(adapter=self.outcome_adapter)
        synced = await service.sync_known_markets(session, limit=cfg.sync_outcome_limit)
        statuses: dict[str, int] = {}
        for row in synced:
            statuses[row.outcome_status] = statuses.get(row.outcome_status, 0) + 1
        return StageResult(
            attempted=len(synced), succeeded=len(synced), summary={"statuses": statuses}
        )

    async def _stage_score(self, session: Session, cfg: BaselineConfig) -> StageResult:
        counts = CalibrationService().score_unscored(session, limit=cfg.score_limit)
        created = counts["scored"] + counts["pending_outcome"] + counts["unscorable"]
        return StageResult(
            attempted=created + counts["skipped"], succeeded=created, summary=counts
        )

    async def _stage_report(self, session: Session, cfg: BaselineConfig) -> StageResult:
        summary = CalibrationService().summary(session)
        return StageResult(
            attempted=summary.total_scores,
            succeeded=summary.total_scores,
            summary={
                "total": summary.total_scores,
                "resolved": summary.resolved,
                "pending_outcome": summary.pending_outcome,
                "unscorable": summary.unscorable,
                "overall": summary.overall.model_dump() if summary.overall else None,
            },
        )

    async def _stage_retention(self, session: Session, cfg: BaselineConfig) -> StageResult:
        from app.services.retention import RetentionService

        counts = RetentionService().prune(session)
        deleted = sum(counts.values())
        return StageResult(attempted=deleted, succeeded=deleted, summary=counts)

    # --- orchestration ----------------------------------------------------

    def _active_run(self, session: Session) -> PipelineRun | None:
        stale_cutoff = _now() - timedelta(seconds=STALE_LOCK_SECONDS)
        candidates = session.execute(
            select(PipelineRun)
            .where(PipelineRun.run_type == RUN_TYPE_BASELINE, PipelineRun.status == "running")
            .order_by(PipelineRun.id.desc())
        ).scalars().all()
        for row in candidates:
            started = row.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if started is not None and started >= stale_cutoff:
                return row
        return None

    async def _run_stage(
        self, session: Session, pipeline_run: PipelineRun, name: str, fn, cfg: BaselineConfig
    ) -> PipelineStageRun:
        stage_started = _now()
        stage = PipelineStageRun(
            pipeline_run_id=pipeline_run.id,
            stage_name=name,
            status="running",
            started_at=stage_started,
            created_at=stage_started,
        )
        session.add(stage)
        session.commit()
        try:
            result = await fn(session, cfg)
            stage.status = "completed"
            stage.items_attempted = result.attempted
            stage.items_succeeded = result.succeeded
            stage.items_failed = result.failed
            stage.summary = result.summary
        except Exception as exc:
            session.rollback()
            logger.exception("Pipeline stage %s failed", name)
            stage.status = "failed"
            stage.error_type = type(exc).__name__
            stage.error_message = str(exc)[:2000]
        stage.finished_at = _now()
        stage.duration_ms = _duration_ms(stage_started, stage.finished_at)
        session.commit()
        return stage

    async def run_baseline_pipeline(
        self, session: Session, config: BaselineConfig | None = None
    ) -> PipelineRun:
        cfg = config or BaselineConfig.from_settings()
        started_at = _now()

        active = self._active_run(session)
        if active is not None:
            skipped = PipelineRun(
                run_type=RUN_TYPE_BASELINE,
                status="skipped",
                started_at=started_at,
                finished_at=started_at,
                duration_ms=0,
                config=asdict(cfg),
                summary={"reason": "already_running", "active_run_id": active.id},
                created_at=started_at,
            )
            session.add(skipped)
            session.commit()
            return skipped

        run = PipelineRun(
            run_type=RUN_TYPE_BASELINE,
            status="running",
            started_at=started_at,
            config=asdict(cfg),
            created_at=started_at,
        )
        session.add(run)
        session.commit()

        if cfg.dry_run:
            for name in BASELINE_STAGES:
                session.add(
                    PipelineStageRun(
                        pipeline_run_id=run.id,
                        stage_name=name,
                        status="skipped",
                        started_at=started_at,
                        finished_at=started_at,
                        duration_ms=0,
                        summary={"dry_run": True},
                        created_at=started_at,
                    )
                )
            run.status = "dry_run"
            run.finished_at = _now()
            run.duration_ms = _duration_ms(started_at, run.finished_at)
            run.summary = {"dry_run": True}
            session.commit()
            return run

        stage_fns = [
            (STAGE_SCAN, self._stage_scan),
            (STAGE_ENRICH, self._stage_enrich),
            (STAGE_ASSESS, self._stage_assess),
            (STAGE_RESEARCH, self._stage_research),
            (STAGE_FORECAST, self._stage_forecast),
            (STAGE_SYNC_OUTCOMES, self._stage_sync_outcomes),
            (STAGE_SCORE, self._stage_score),
            (STAGE_REPORT, self._stage_report),
        ]
        if cfg.run_retention:
            stage_fns.append((STAGE_RETENTION, self._stage_retention))
        stage_statuses: dict[str, str] = {}
        any_failed = False
        try:
            for name, fn in stage_fns:
                stage = await self._run_stage(session, run, name, fn, cfg)
                stage_statuses[name] = stage.status
                if stage.status == "failed":
                    any_failed = True
                    if cfg.fail_fast:
                        break
            if any_failed:
                run.status = "failed" if cfg.fail_fast else "completed_with_errors"
            else:
                run.status = "completed"
        except Exception as exc:  # orchestration-level failure
            session.rollback()
            logger.exception("Baseline pipeline crashed")
            run.status = "failed"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
        run.finished_at = _now()
        run.duration_ms = _duration_ms(started_at, run.finished_at)
        run.summary = {"stages": stage_statuses}
        session.commit()
        return run
