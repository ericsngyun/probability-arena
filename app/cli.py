"""Probability Arena CLI.

Usage:
    python -m app.cli scan --limit 100
    python -m app.cli enrich-details --limit 20
    python -m app.cli assess-resolution --limit 20
    python -m app.cli collect-research --limit 10
    python -m app.cli forecast --limit 10
    python -m app.cli sync-outcomes --limit 100
    python -m app.cli score-forecasts --limit 500
    python -m app.cli calibration-report
    python -m app.cli run-baseline
    python -m app.cli pipeline-status
    python -m app.cli watch-once --limit 100
    python -m app.cli watch-loop --interval 60 --limit 100
    python -m app.cli prune-retention [--dry-run]
    python -m app.cli db-stats

Read-only: `scan` fetches public Kalshi market data, ranks it, and persists
snapshots; `enrich-details` fetches detail/event/series metadata for top
eligible candidates; `assess-resolution` scores resolution clarity (using
enriched metadata where available); `collect-research` builds structured
evidence packets; `forecast` turns packets into probability forecasts with
capped confidence. Recommended sequence:
scan -> enrich-details -> assess-resolution -> collect-research -> forecast
-> sync-outcomes -> score-forecasts -> calibration-report.
Forecasts are probabilities and reasoning artifacts only; calibration is
read-only scoring against observed outcomes. There are no trading commands.
"""

import argparse
import asyncio
import logging
import sys

from app.adapters.kalshi import KalshiRestAdapter
from app.models import ScannerRun

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

TOP_N_PRINTED = 20


async def scan(
    limit: int | None = None,
    adapter: KalshiRestAdapter | None = None,
    session=None,
) -> ScannerRun:
    """Run one scan and print a summary. When no session is injected, runs
    migrations and opens one against the configured DATABASE_URL."""
    from app.services.scanner import run_scan

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        result = await run_scan(session, adapter=adapter, max_markets=limit, source="cli")
    finally:
        if owns_session:
            session.close()

    run = result.run
    print(
        f"scan run={run.id} status={run.status} source={run.source} "
        f"fetched={run.markets_fetched} eligible={len(result.ranked)} "
        f"rejected={len(result.rejected)} duration_ms={run.duration_ms}"
    )
    for position, item in enumerate(result.ranked[:TOP_N_PRINTED], start=1):
        print(f"{position:>3}. {item.score:.4f}  {item.market.ticker:<30} {item.market.title[:60]}")
    if result.rejected:
        reason_counts: dict[str, int] = {}
        for _, assessment in result.rejected:
            for reason in assessment.rejection_reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        summary = ", ".join(f"{reason}={count}" for reason, count in sorted(reason_counts.items()))
        print(f"rejections: {summary}")
    return run


async def assess_resolution(
    limit: int = 20,
    judge=None,
    session=None,
) -> int:
    """Assess resolution criteria for the top eligible candidates of the most
    recent successful scan (running a fresh scan if none exists), persist the
    assessments linked to that scan, and print a summary. Returns the number
    of markets assessed."""
    from sqlalchemy import select

    from app.models import Market, MarketSnapshot, ScannerRun
    from app.schemas import MarketData
    from app.services.enrichment import apply_latest_enrichment
    from app.services.resolution import get_judge, persist_assessment
    from app.services.scanner import run_scan

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        judge = judge or get_judge()
        run = session.execute(
            select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
        ).scalars().first()
        if run is None:
            print("no prior scan found; running a fresh scan")
            result = await run_scan(session, source="cli")
            run = result.run

        rows = session.execute(
            select(MarketSnapshot, Market)
            .join(Market, MarketSnapshot.market_id == Market.id)
            .where(MarketSnapshot.scanner_run_id == run.id, MarketSnapshot.score > 0)
            .order_by(MarketSnapshot.score.desc())
            .limit(limit)
        ).all()
        if not rows:
            print(f"scan run {run.id} has no eligible candidates to assess")
            return 0

        print(f"assessing {len(rows)} candidates from scan run {run.id} judge={judge.model_name}")
        for snapshot, market in rows:
            market_data = MarketData(
                ticker=market.ticker,
                event_ticker=market.event_ticker,
                title=market.title or "",
                category=market.category,
                status=market.status,
                yes_bid=snapshot.yes_bid,
                yes_ask=snapshot.yes_ask,
                volume_24h=snapshot.volume_24h,
                open_interest=snapshot.open_interest,
                liquidity=snapshot.liquidity,
                close_time=market.close_time,
                expiration_time=market.expiration_time,
                rules_primary=market.rules_primary,
            )
            market_data = apply_latest_enrichment(session, market_data)
            assessment = await judge.assess(market_data)
            persist_assessment(session, market.ticker, assessment, judge, scanner_run_id=run.id)
            print(
                f"  {market.ticker:<40} clarity={assessment.clarity_score:.2f} "
                f"risk={assessment.resolution_risk} tradeability={assessment.tradeability}"
            )
        return len(rows)
    finally:
        if owns_session:
            session.close()


async def enrich_details(
    limit: int = 20,
    adapter=None,
    session=None,
) -> int:
    """Enrich detail/event/series metadata for the top eligible candidates of
    the most recent successful scan (running a fresh scan if none exists).
    Returns the number of markets enriched."""
    from sqlalchemy import select

    from app.models import ScannerRun
    from app.services.enrichment import MarketDetailEnrichmentService
    from app.services.scanner import run_scan

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        run = session.execute(
            select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
        ).scalars().first()
        if run is None:
            print("no prior scan found; running a fresh scan")
            result = await run_scan(session, adapter=adapter, source="cli")
            run = result.run

        service = MarketDetailEnrichmentService(adapter=adapter)
        enriched = await service.enrich_top_candidates(session, run_id=run.id, limit=limit)
        print(f"enriched {len(enriched)} candidates from scan run {run.id}")
        for row in enriched:
            source = row.settlement_source or "-"
            print(f"  {row.market_ticker:<40} series={row.series_ticker or '-':<16} source={source[:70]}")
        return len(enriched)
    finally:
        if owns_session:
            session.close()


async def collect_research(
    limit: int = 10,
    collector=None,
    session=None,
    prepare: bool = False,
) -> int:
    """Build research packets for the top eligible candidates of the most
    recent successful scan, preferring markets that already have an enrichment
    and a researchable resolution. By default nothing upstream is triggered;
    with prepare=True, missing enrichments/assessments are created first.
    Returns the number of packets persisted."""
    from sqlalchemy import select

    from app.models import Market, MarketSnapshot, ScannerRun
    from app.services.enrichment import (
        EnrichmentError,
        MarketDetailEnrichmentService,
        latest_enrichment_for,
    )
    from app.services.research import create_research_packet, get_collector
    from app.services.resolution import get_judge, latest_assessment_for, persist_assessment

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        run = session.execute(
            select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
        ).scalars().first()
        if run is None:
            print("no successful scan found; run `python -m app.cli scan` first")
            return 0

        rows = session.execute(
            select(MarketSnapshot, Market)
            .join(Market, MarketSnapshot.market_id == Market.id)
            .where(MarketSnapshot.scanner_run_id == run.id, MarketSnapshot.score > 0)
            .order_by(MarketSnapshot.score.desc())
            .limit(limit)
        ).all()
        if not rows:
            print(f"scan run {run.id} has no eligible candidates")
            return 0

        if prepare:
            enrichment_service = MarketDetailEnrichmentService()
            judge = get_judge()
            from app.schemas import MarketData
            from app.services.enrichment import apply_latest_enrichment

            for _, market in rows:
                if latest_enrichment_for(session, market.ticker) is None:
                    try:
                        await enrichment_service.enrich_ticker(
                            session, market.ticker, scanner_run_id=run.id
                        )
                    except EnrichmentError as exc:
                        print(f"  prepare: enrichment failed for {market.ticker}: {exc}")
                if latest_assessment_for(session, market.ticker) is None:
                    market_data = apply_latest_enrichment(
                        session,
                        MarketData(
                            ticker=market.ticker,
                            title=market.title or "",
                            status=market.status,
                            close_time=market.close_time,
                            rules_primary=market.rules_primary,
                        ),
                    )
                    assessment = await judge.assess(market_data)
                    persist_assessment(
                        session, market.ticker, assessment, judge, scanner_run_id=run.id
                    )

        # Prefer markets that are fully prepared: enrichment present and
        # latest resolution researchable; then by score.
        def preparedness(market: Market) -> int:
            enriched = latest_enrichment_for(session, market.ticker) is not None
            resolution = latest_assessment_for(session, market.ticker)
            researchable = resolution is not None and resolution.tradeability == "researchable"
            return 0 if (enriched and researchable) else 1

        ordered = sorted(
            rows, key=lambda pair: (preparedness(pair[1]), -(pair[0].score or 0.0))
        )

        collector = collector or get_collector()
        domain_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        print(
            f"collecting research for {len(ordered)} candidates from scan run {run.id} "
            f"collector={collector.name}"
        )
        for _, market in ordered:
            packet_row = await create_research_packet(
                session, market, collector=collector, scanner_run_id=run.id
            )
            domain_counts[packet_row.domain] = domain_counts.get(packet_row.domain, 0) + 1
            risk_counts[packet_row.research_risk] = (
                risk_counts.get(packet_row.research_risk, 0) + 1
            )
            print(
                f"  {market.ticker:<40} domain={packet_row.domain:<16} "
                f"completeness={packet_row.research_completeness_score:.2f} "
                f"risk={packet_row.research_risk}"
            )
        print("domains: " + ", ".join(f"{d}={n}" for d, n in sorted(domain_counts.items())))
        print("risk: " + ", ".join(f"{r}={n}" for r, n in sorted(risk_counts.items())))
        return len(ordered)
    finally:
        if owns_session:
            session.close()


async def forecast(
    limit: int = 10,
    forecaster=None,
    session=None,
    prepare: bool = False,
) -> int:
    """Create forecasts for the top eligible candidates of the most recent
    successful scan that already have research packets, preferring markets
    that are also enriched and resolution-assessed as researchable. By default
    markets without packets are skipped; with prepare=True, missing packets
    (and their upstream rows) are created first. Returns forecasts persisted."""
    from sqlalchemy import select

    from app.models import Market, MarketSnapshot, ScannerRun
    from app.services.enrichment import latest_enrichment_for
    from app.services.forecasting import ForecastingService
    from app.services.research import create_research_packet, latest_packet_for
    from app.services.resolution import latest_assessment_for

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        run = session.execute(
            select(ScannerRun).where(ScannerRun.status == "ok").order_by(ScannerRun.id.desc())
        ).scalars().first()
        if run is None:
            print("no successful scan found; run `python -m app.cli scan` first")
            return 0

        rows = session.execute(
            select(MarketSnapshot, Market)
            .join(Market, MarketSnapshot.market_id == Market.id)
            .where(MarketSnapshot.scanner_run_id == run.id, MarketSnapshot.score > 0)
            .order_by(MarketSnapshot.score.desc())
            .limit(limit)
        ).all()
        if not rows:
            print(f"scan run {run.id} has no eligible candidates")
            return 0

        if prepare:
            for _, market in rows:
                if latest_packet_for(session, market.ticker) is None:
                    await create_research_packet(session, market, scanner_run_id=run.id)

        with_packets = [
            pair for pair in rows if latest_packet_for(session, pair[1].ticker) is not None
        ]
        skipped = len(rows) - len(with_packets)
        if skipped:
            print(f"skipping {skipped} candidates without research packets (use --prepare)")
        if not with_packets:
            print("no candidates with research packets; run collect-research first")
            return 0

        def preparedness(market: Market) -> int:
            enriched = latest_enrichment_for(session, market.ticker) is not None
            resolution = latest_assessment_for(session, market.ticker)
            researchable = resolution is not None and resolution.tradeability == "researchable"
            return 0 if (enriched and researchable) else 1

        ordered = sorted(
            with_packets, key=lambda pair: (preparedness(pair[1]), -(pair[0].score or 0.0))
        )

        service = ForecastingService(forecaster=forecaster)
        domain_counts: dict[str, int] = {}
        depth_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        print(
            f"forecasting {len(ordered)} candidates from scan run {run.id} "
            f"forecaster={service.forecaster.name}"
        )
        for _, market in ordered:
            row = await service.forecast_market(session, market, scanner_run_id=run.id)
            packet = latest_packet_for(session, market.ticker)
            domain = packet.domain if packet else "general"
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            depth_counts[row.evidence_depth] = depth_counts.get(row.evidence_depth, 0) + 1
            risk_counts[row.forecast_risk] = risk_counts.get(row.forecast_risk, 0) + 1
            print(
                f"  {market.ticker:<40} p={row.estimated_probability:.2f} "
                f"conf={row.confidence:.2f} depth={row.evidence_depth} risk={row.forecast_risk}"
            )
        print("domains: " + ", ".join(f"{d}={n}" for d, n in sorted(domain_counts.items())))
        print("evidence: " + ", ".join(f"{d}={n}" for d, n in sorted(depth_counts.items())))
        print("risk: " + ", ".join(f"{r}={n}" for r, n in sorted(risk_counts.items())))
        return len(ordered)
    finally:
        if owns_session:
            session.close()


async def sync_outcomes(
    limit: int = 100,
    adapter=None,
    session=None,
) -> int:
    """Sync settlement state for known markets (forecasted tickers first).
    Returns the number of outcomes synced."""
    from app.services.outcomes import OutcomeService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        service = OutcomeService(adapter=adapter)
        synced = await service.sync_known_markets(session, limit=limit)
        status_counts: dict[str, int] = {}
        for row in synced:
            status_counts[row.outcome_status] = status_counts.get(row.outcome_status, 0) + 1
        print(f"synced {len(synced)} outcomes")
        if status_counts:
            print("status: " + ", ".join(f"{s}={n}" for s, n in sorted(status_counts.items())))
        return len(synced)
    finally:
        if owns_session:
            session.close()


async def score_forecasts(
    limit: int = 500,
    session=None,
) -> int:
    """Score forecasts against synced outcomes. Returns rows created."""
    from app.services.calibration import CalibrationService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        counts = CalibrationService().score_unscored(session, limit=limit)
        created = counts["scored"] + counts["pending_outcome"] + counts["unscorable"]
        print(
            f"scored={counts['scored']} pending_outcome={counts['pending_outcome']} "
            f"unscorable={counts['unscorable']} skipped={counts['skipped']}"
        )
        return created
    finally:
        if owns_session:
            session.close()


async def calibration_report(session=None) -> int:
    """Print the aggregate calibration summary by cohort. Returns the number
    of forecasts covered by the latest-score summary."""
    from app.services.calibration import CalibrationService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        summary = CalibrationService().summary(session)
        print(
            f"calibration: total={summary.total_scores} resolved={summary.resolved} "
            f"pending={summary.pending_outcome} unscorable={summary.unscorable}"
        )
        if summary.overall:
            print(
                f"overall: brier={summary.overall.mean_brier} "
                f"log_loss={summary.overall.mean_log_loss} "
                f"abs_error={summary.overall.mean_absolute_error} "
                f"(n={summary.overall.count})"
            )
        for label, cohorts in (
            ("evidence", summary.by_evidence_depth),
            ("risk", summary.by_forecast_risk),
            ("forecaster", summary.by_forecaster),
            ("domain", summary.by_domain),
        ):
            for name, stats in sorted(cohorts.items()):
                print(
                    f"  {label}={name:<20} n={stats.count:<4} brier={stats.mean_brier} "
                    f"log_loss={stats.mean_log_loss} abs_error={stats.mean_absolute_error}"
                )
        return summary.total_scores
    finally:
        if owns_session:
            session.close()


async def run_baseline(
    scan_limit: int | None = None,
    candidate_limit: int | None = None,
    sync_outcome_limit: int | None = None,
    score_limit: int | None = None,
    fail_fast: bool | None = None,
    dry_run: bool = False,
    runner=None,
    session=None,
):
    """Execute the full read-only measurement loop as one audited pipeline
    run and print a compact stage summary. Returns the PipelineRun row."""
    from app.services.pipeline import BaselineConfig, PipelineRunner

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        config = BaselineConfig.from_settings(
            scan_limit=scan_limit,
            candidate_limit=candidate_limit,
            sync_outcome_limit=sync_outcome_limit,
            score_limit=score_limit,
            fail_fast=fail_fast,
        )
        config.dry_run = dry_run
        runner = runner or PipelineRunner()
        run = await runner.run_baseline_pipeline(session, config)

        print(f"pipeline run={run.id} status={run.status} duration_ms={run.duration_ms}")
        if run.status == "skipped":
            print(f"  reason: {run.summary.get('reason')} (run {run.summary.get('active_run_id')})")
            return run
        for stage in run.stages:
            error = f" error={stage.error_type}" if stage.error_type else ""
            print(
                f"  {stage.stage_name:<20} {stage.status:<22} "
                f"ok={stage.items_succeeded}/{stage.items_attempted} "
                f"failed={stage.items_failed} {stage.duration_ms or 0}ms{error}"
            )
        return run
    finally:
        if owns_session:
            session.close()


async def pipeline_status(limit: int = 5, session=None) -> int:
    """Print recent pipeline runs and the latest run's stage table. Returns
    the number of runs printed."""
    from sqlalchemy import select

    from app.models import PipelineRun

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        runs = session.execute(
            select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit)
        ).scalars().all()
        if not runs:
            print("no pipeline runs recorded")
            return 0
        for run in runs:
            print(
                f"run={run.id} type={run.run_type} status={run.status} "
                f"started={run.started_at} duration_ms={run.duration_ms}"
            )
        latest = runs[0]
        if latest.stages:
            print(f"stages of run {latest.id}:")
            for stage in latest.stages:
                error = f" error={stage.error_type}" if stage.error_type else ""
                print(
                    f"  {stage.stage_name:<20} {stage.status:<22} "
                    f"ok={stage.items_succeeded}/{stage.items_attempted}{error}"
                )
        return len(runs)
    finally:
        if owns_session:
            session.close()


async def watch_once(
    limit: int | None = None,
    adapter=None,
    session=None,
):
    """One read-only watcher pass: record price ticks and informational
    opportunity signals for the candidate universe. Returns the WatcherRun."""
    from app.services.watcher import RealtimeWatcher

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        watcher = RealtimeWatcher(adapter=adapter)
        run = await watcher.watch_once(session, limit=limit)
        print(
            f"watcher run={run.id} status={run.status} markets={run.markets_checked} "
            f"ticks={run.ticks_recorded} signals={run.signals_created} "
            f"duration_ms={run.duration_ms}"
        )
        if run.signals_created:
            from sqlalchemy import select

            from app.models import OpportunitySignal

            signals = session.execute(
                select(OpportunitySignal)
                .order_by(OpportunitySignal.id.desc())
                .limit(run.signals_created)
            ).scalars().all()
            for signal in reversed(signals):
                print(f"  [{signal.signal_type}] {signal.market_ticker}: {signal.reason}")
        return run
    finally:
        if owns_session:
            session.close()


async def watch_loop(
    interval: int | None = None,
    limit: int | None = None,
    adapter=None,
    session=None,
    max_iterations: int | None = None,
) -> int:
    """Run watcher passes on an interval until SIGINT/SIGTERM (or
    max_iterations, for tests). Requires ENABLE_REALTIME_WATCHER=true.
    Per-pass errors are printed and the loop continues. Returns iterations."""
    import asyncio as aio
    import signal as os_signal

    from app.config import get_settings
    from app.services.watcher import RealtimeWatcher

    settings = get_settings()
    if not settings.enable_realtime_watcher:
        print("ENABLE_REALTIME_WATCHER=false; set it to true in .env to run the loop")
        return 0
    if interval is None:
        interval = settings.watcher_poll_interval_seconds

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()

    stop = aio.Event()
    loop = aio.get_running_loop()
    installed_handlers = []
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed_handlers.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    watcher = RealtimeWatcher(adapter=adapter)
    iterations = 0
    print(f"watcher loop started (interval={interval}s); Ctrl-C to stop")
    try:
        while not stop.is_set():
            if owns_session:
                from app.db import get_sessionmaker

                iteration_session = get_sessionmaker()()
            else:
                iteration_session = session
            try:
                run = await watcher.watch_once(iteration_session, limit=limit)
                print(
                    f"watcher run={run.id} status={run.status} markets={run.markets_checked} "
                    f"signals={run.signals_created}"
                )
            except Exception as exc:
                print(f"watcher pass failed: {type(exc).__name__}: {exc}")
            finally:
                if owns_session:
                    iteration_session.close()
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            try:
                await aio.wait_for(stop.wait(), timeout=interval)
            except aio.TimeoutError:
                pass
    finally:
        for sig in installed_handlers:
            loop.remove_signal_handler(sig)
    print(f"watcher loop stopped after {iterations} iteration(s)")
    return iterations


async def prune_retention(
    dry_run: bool = False,
    tick_days: int | None = None,
    watcher_run_days: int | None = None,
    pipeline_run_days: int | None = None,
    signal_days: int | None = None,
    batch_size: int | None = None,
    session=None,
) -> int:
    """Prune operational tables per retention windows (intelligence and
    calibration tables are never touched). Returns total rows deleted (or
    that would be deleted, when dry_run)."""
    from app.services.retention import RetentionConfig, RetentionService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        config = RetentionConfig.from_settings(
            tick_days=tick_days,
            watcher_run_days=watcher_run_days,
            pipeline_run_days=pipeline_run_days,
            signal_days=signal_days,
            batch_size=batch_size,
        )
        counts = RetentionService(config).prune(session, dry_run=dry_run)
        mode = "DRY RUN — would delete" if dry_run else "deleted"
        print(f"retention ({mode}):")
        for table, count in counts.items():
            note = " (retention disabled)" if table == "opportunity_signals" and config.signal_days == 0 else ""
            print(f"  {table:<24} {count}{note}")
        return sum(counts.values())
    finally:
        if owns_session:
            session.close()


async def db_stats(session=None) -> int:
    """Print database overview: redacted URL, table row counts, size (SQLite),
    latest watcher/pipeline runs, signal counts. Returns total rows counted."""
    from sqlalchemy import func, select
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import Base
    from app.models import OpportunitySignal, PipelineRun, WatcherRun

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        url = make_url(get_settings().database_url)
        print(f"database: {url.render_as_string(hide_password=True)}")
        if url.get_backend_name() == "sqlite" and url.database:
            import os

            if os.path.exists(url.database):
                size_mb = os.path.getsize(url.database) / (1024 * 1024)
                print(f"sqlite size: {size_mb:.2f} MiB")

        total = 0
        print("row counts:")
        for table in sorted(Base.metadata.tables):
            count = session.execute(
                select(func.count()).select_from(Base.metadata.tables[table])
            ).scalar() or 0
            total += count
            print(f"  {table:<36} {count}")

        latest_watcher = session.execute(
            select(WatcherRun).order_by(WatcherRun.id.desc())
        ).scalars().first()
        if latest_watcher:
            print(
                f"latest watcher run: id={latest_watcher.id} status={latest_watcher.status} "
                f"markets={latest_watcher.markets_checked} signals={latest_watcher.signals_created} "
                f"started={latest_watcher.started_at}"
            )
        latest_pipeline = session.execute(
            select(PipelineRun).order_by(PipelineRun.id.desc())
        ).scalars().first()
        if latest_pipeline:
            print(
                f"latest pipeline run: id={latest_pipeline.id} status={latest_pipeline.status} "
                f"started={latest_pipeline.started_at}"
            )
        by_status = session.execute(
            select(OpportunitySignal.signal_status, func.count()).group_by(
                OpportunitySignal.signal_status
            )
        ).all()
        by_type = session.execute(
            select(OpportunitySignal.signal_type, func.count()).group_by(
                OpportunitySignal.signal_type
            )
        ).all()
        if by_status:
            print("signals by status: " + ", ".join(f"{s}={n}" for s, n in sorted(by_status)))
        if by_type:
            print("signals by type: " + ", ".join(f"{t}={n}" for t, n in sorted(by_type)))
        return total
    finally:
        if owns_session:
            session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="Probability Arena — read-only Kalshi market intelligence CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="Fetch, rank, and persist active markets")
    scan_parser.add_argument(
        "--limit", type=int, default=None, help="Max markets to fetch (default: SCANNER_MAX_MARKETS)"
    )
    assess_parser = subparsers.add_parser(
        "assess-resolution",
        help="Assess resolution clarity for top eligible candidates of the latest scan",
    )
    assess_parser.add_argument(
        "--limit", type=int, default=20, help="Max candidates to assess (default: 20)"
    )
    enrich_parser = subparsers.add_parser(
        "enrich-details",
        help="Fetch detail/event/series metadata for top eligible candidates of the latest scan",
    )
    enrich_parser.add_argument(
        "--limit", type=int, default=20, help="Max candidates to enrich (default: 20)"
    )
    research_parser = subparsers.add_parser(
        "collect-research",
        help="Build research packets for top eligible candidates of the latest scan",
    )
    research_parser.add_argument(
        "--limit", type=int, default=10, help="Max candidates to research (default: 10)"
    )
    research_parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create missing enrichments/resolution assessments first (off by default)",
    )
    forecast_parser = subparsers.add_parser(
        "forecast",
        help="Create probability forecasts for candidates with research packets",
    )
    forecast_parser.add_argument(
        "--limit", type=int, default=10, help="Max candidates to forecast (default: 10)"
    )
    forecast_parser.add_argument(
        "--prepare",
        action="store_true",
        help="Create missing research packets first (off by default)",
    )
    sync_parser = subparsers.add_parser(
        "sync-outcomes", help="Sync settlement state for known markets (read-only)"
    )
    sync_parser.add_argument(
        "--limit", type=int, default=100, help="Max markets to sync (default: 100)"
    )
    score_parser = subparsers.add_parser(
        "score-forecasts", help="Score forecasts against synced outcomes"
    )
    score_parser.add_argument(
        "--limit", type=int, default=500, help="Max forecasts to consider (default: 500)"
    )
    subparsers.add_parser(
        "calibration-report", help="Print aggregate calibration summary by cohort"
    )
    baseline_parser = subparsers.add_parser(
        "run-baseline", help="Run the full read-only measurement loop as one audited pipeline"
    )
    baseline_parser.add_argument("--scan-limit", type=int, default=None)
    baseline_parser.add_argument("--candidate-limit", type=int, default=None)
    baseline_parser.add_argument("--sync-outcome-limit", type=int, default=None)
    baseline_parser.add_argument("--score-limit", type=int, default=None)
    baseline_parser.add_argument(
        "--fail-fast", action="store_true", default=None, help="Stop at the first failed stage"
    )
    baseline_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Record the pipeline audit row without executing any stage",
    )
    status_parser = subparsers.add_parser(
        "pipeline-status", help="Show recent pipeline runs and latest stage summaries"
    )
    status_parser.add_argument("--limit", type=int, default=5)
    watch_once_parser = subparsers.add_parser(
        "watch-once", help="One read-only watcher pass (ticks + informational signals)"
    )
    watch_once_parser.add_argument("--limit", type=int, default=None)
    watch_loop_parser = subparsers.add_parser(
        "watch-loop", help="Poll the watcher on an interval (requires ENABLE_REALTIME_WATCHER)"
    )
    watch_loop_parser.add_argument("--interval", type=int, default=None)
    watch_loop_parser.add_argument("--limit", type=int, default=None)
    prune_parser = subparsers.add_parser(
        "prune-retention", help="Prune operational tables per retention windows"
    )
    prune_parser.add_argument("--dry-run", action="store_true")
    prune_parser.add_argument("--tick-days", type=int, default=None)
    prune_parser.add_argument("--watcher-run-days", type=int, default=None)
    prune_parser.add_argument("--pipeline-run-days", type=int, default=None)
    prune_parser.add_argument("--signal-days", type=int, default=None)
    prune_parser.add_argument("--batch-size", type=int, default=None)
    subparsers.add_parser("db-stats", help="Print database overview and row counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "scan":
        run = asyncio.run(scan(limit=args.limit))
        return 0 if run.status == "ok" else 1
    if args.command == "assess-resolution":
        assessed = asyncio.run(assess_resolution(limit=args.limit))
        return 0 if assessed >= 0 else 1
    if args.command == "enrich-details":
        enriched = asyncio.run(enrich_details(limit=args.limit))
        return 0 if enriched >= 0 else 1
    if args.command == "collect-research":
        collected = asyncio.run(collect_research(limit=args.limit, prepare=args.prepare))
        return 0 if collected >= 0 else 1
    if args.command == "forecast":
        forecasted = asyncio.run(forecast(limit=args.limit, prepare=args.prepare))
        return 0 if forecasted >= 0 else 1
    if args.command == "sync-outcomes":
        synced = asyncio.run(sync_outcomes(limit=args.limit))
        return 0 if synced >= 0 else 1
    if args.command == "score-forecasts":
        scored = asyncio.run(score_forecasts(limit=args.limit))
        return 0 if scored >= 0 else 1
    if args.command == "calibration-report":
        total = asyncio.run(calibration_report())
        return 0 if total >= 0 else 1
    if args.command == "run-baseline":
        run = asyncio.run(
            run_baseline(
                scan_limit=args.scan_limit,
                candidate_limit=args.candidate_limit,
                sync_outcome_limit=args.sync_outcome_limit,
                score_limit=args.score_limit,
                fail_fast=args.fail_fast,
                dry_run=args.dry_run,
            )
        )
        return 0 if run.status != "failed" else 1
    if args.command == "pipeline-status":
        count = asyncio.run(pipeline_status(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "watch-once":
        run = asyncio.run(watch_once(limit=args.limit))
        return 0 if run.status == "ok" else 1
    if args.command == "watch-loop":
        iterations = asyncio.run(watch_loop(interval=args.interval, limit=args.limit))
        return 0 if iterations >= 0 else 1
    if args.command == "prune-retention":
        deleted = asyncio.run(
            prune_retention(
                dry_run=args.dry_run,
                tick_days=args.tick_days,
                watcher_run_days=args.watcher_run_days,
                pipeline_run_days=args.pipeline_run_days,
                signal_days=args.signal_days,
                batch_size=args.batch_size,
            )
        )
        return 0 if deleted >= 0 else 1
    if args.command == "db-stats":
        total = asyncio.run(db_stats())
        return 0 if total >= 0 else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
