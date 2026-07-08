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
    python -m app.cli signals-recent --limit 20
    python -m app.cli promote-signals --limit 5
    python -m app.cli process-promoted-signals --limit 5
    python -m app.cli signal-report
    python -m app.cli research-canary-report
    python -m app.cli champion-challenger-report

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
    if result.targeted is not None:
        t = result.targeted
        by_series = ", ".join(f"{s}={n}" for s, n in sorted(t.by_series.items())) or "none"
        print(
            f"targeted scan (SCANNER-002): generic={t.generic_fetched} "
            f"targeted_fetched={t.targeted_fetched} added_after_dedupe={t.targeted_added}"
        )
        print(f"  by series: {by_series}")
        if t.failed_series:
            failed = ", ".join(f"{s}({e})" for s, e in sorted(t.failed_series.items()))
            print(f"  failed series (scan continued): {failed}")
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
        if len(summary.by_forecaster) > 1:
            print("hint: run `champion-challenger-report` for head-to-head forecaster comparison")
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
        service = RetentionService(config)
        counts = service.prune(session, dry_run=dry_run)
        mode = "DRY RUN — would delete" if dry_run else "deleted"
        print(f"retention ({mode}):")
        for table, count in counts.items():
            note = " (retention disabled)" if table == "opportunity_signals" and config.signal_days == 0 else ""
            print(f"  {table:<24} {count}{note}")
        if dry_run:
            print("\nretention detail (OPS-011; dry-run projection, nothing deleted):")
            for r in service.prune_report(session):
                window = "keep forever" if r.window_days is None else f"{r.window_days}d window"
                line = (
                    f"  {r.table:<24} {window:<14} total={r.total_rows} "
                    f"eligible={r.eligible_rows} remaining={r.remaining_rows}"
                )
                if r.oldest is not None or r.newest is not None:
                    line += f" oldest={r.oldest} newest={r.newest}"
                print(line)
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
        from app.services.backup import backup_dir_stats

        stats = backup_dir_stats()
        if stats is not None:
            count, total_mb = stats
            print(f"backups: {count} file(s), {total_mb:.2f} MiB")

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


async def db_growth_report(top: int = 12, session=None) -> int:
    """OPS-011 read-only DB growth/retention observability: size, table row
    counts + est MiB (dbstat when available), largest tables, tick age
    buckets, ticks-by-domain, edge-precheck/crypto row growth, backups,
    retention windows, and calibrated alert thresholds. Returns total rows."""
    from app.services.db_growth import build_growth_report

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = build_growth_report(session)
        print(f"database: {r.database_url}")
        if r.size_mib is not None:
            print(f"sqlite size: {r.size_mib:.2f} MiB")
        if r.backups is not None:
            print(f"backups: {r.backups[0]} file(s), {r.backups[1]:.2f} MiB")

        total_rows = sum(t.rows for t in r.tables)
        has_size = any(t.est_mib is not None for t in r.tables)
        print(f"\nlargest tables (of {len(r.tables)}; total {total_rows} rows"
              f"{'' if has_size else '; est MiB unavailable — dbstat not compiled in'}):")
        for t in r.largest_tables[:top]:
            size = f"  {t.est_mib:>8.2f} MiB" if t.est_mib is not None else ""
            print(f"  {t.name:<36} {t.rows:>9} rows{size}")

        print(f"\nmarket_price_ticks: {r.tick_total} rows"
              f" (oldest={r.tick_oldest} newest={r.tick_newest})")
        print("  by age:    " + ", ".join(f"{k}={v}" for k, v in r.tick_by_age.items()))
        print("  by domain: " + ", ".join(f"{k}={v}" for k, v in r.tick_by_domain.items()))
        print(f"  last hour: {r.tick_last_hour} ticks"
              + (f"  (~{r.tick_est_daily_mib} MiB/day est)"
                 if r.tick_est_daily_mib is not None else ""))

        print(f"\nedge_precheck_snapshots: {r.edge_total} total"
              f"  (+{r.edge_last_hour}/h, +{r.edge_last_24h}/24h)")
        print(f"crypto_price_ticks: {r.crypto_tick_total} total (+{r.crypto_tick_last_hour}/h)"
              f"   crypto_token_risk_assessments: {r.crypto_risk_total} total")
        print(f"meme_attention_snapshots: {r.meme_attention_total} total "
              f"(+{r.meme_attention_last_hour}/h)   "
              f"meme_catalyst_events: {r.meme_catalyst_total} total")

        rc = r.retention
        print("\nretention windows: "
              f"ticks={rc.tick_days}d, crypto={rc.crypto_days}d, meme={rc.meme_days}d, "
              f"watcher_runs={rc.watcher_run_days}d, pipeline_runs={rc.pipeline_run_days}d, "
              f"signals={'keep forever' if rc.signal_days == 0 else str(rc.signal_days) + 'd'}, "
              "edge_precheck_snapshots=keep forever")
        th = r.thresholds
        print("alert thresholds (OPS-011): "
              f"db_growth warn={th['db_growth_warning_mb']:.0f}MiB "
              f"crit={th['db_growth_critical_mb']:.0f}MiB "
              f"(rate obs: {th['db_growth_warning_daily_mb']:.0f}MiB/day over "
              f"{th['db_growth_window_hours']}h); "
              f"signal_flood warn={th['signal_flood_warning_per_hour']}/h "
              f"crit={th['signal_flood_critical_per_hour']}/h")
        return total_rows
    finally:
        if owns_session:
            session.close()


async def signals_recent(limit: int = 20, signal_status: str | None = None, session=None) -> int:
    """Print recent signals, newest first. Returns the number printed."""
    from app.services.signal_workflow import SignalPromotionService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        rows = SignalPromotionService().list_recent(session, limit=limit, signal_status=signal_status)
        if not rows:
            print("no signals recorded")
            return 0
        for signal in rows:
            mids = ""
            if signal.old_midpoint is not None and signal.new_midpoint is not None:
                mids = f" {signal.old_midpoint:.2f}->{signal.new_midpoint:.2f}"
            print(
                f"  #{signal.id:<5} [{signal.signal_status:<22}] {signal.signal_type:<28} "
                f"{signal.market_ticker}{mids}"
            )
        return len(rows)
    finally:
        if owns_session:
            session.close()


async def promote_signals(limit: int = 5, session=None) -> int:
    """Promote top-N 'new' signals by deterministic priority. Returns the
    number promoted."""
    from app.services.signal_workflow import SignalPromotionService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        promoted = SignalPromotionService().promote_top(session, limit=limit)
        print(f"promoted {len(promoted)} signal(s)")
        for signal in promoted:
            print(f"  #{signal.id} {signal.signal_type:<28} {signal.market_ticker}")
        return len(promoted)
    finally:
        if owns_session:
            session.close()


async def process_promoted_signals(limit: int = 5, services=None, session=None) -> int:
    """Refresh enrichment/assessment/research/forecast for promoted signals.
    Returns the number processed (including failures, which are recorded on
    the signal)."""
    from app.services.signal_workflow import SignalProcessingService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        from app.services.signal_workflow import refreshed_packet_summary

        service = services or SignalProcessingService()
        processed = await service.process_promoted(session, limit=limit)
        print(f"processed {len(processed)} promoted signal(s)")
        for signal in processed:
            if signal.processing_error_type:
                print(
                    f"  #{signal.id} {signal.market_ticker}: FAILED "
                    f"{signal.processing_error_type}: {signal.processing_error_message}"
                )
            else:
                summary = refreshed_packet_summary(session, signal)
                research = (
                    f" research={summary.collector_name}/{summary.evidence_depth} "
                    f"completeness={summary.research_completeness_score:.2f}"
                    if summary
                    else ""
                )
                print(
                    f"  #{signal.id} {signal.market_ticker}: {signal.signal_status} "
                    f"packet={signal.refreshed_research_packet_id} "
                    f"forecast={signal.refreshed_forecast_id}{research}"
                )
        return len(processed)
    finally:
        if owns_session:
            session.close()


async def signal_report(session=None) -> int:
    """Print the aggregate signal-workflow report. Returns total signals."""
    from app.services.signal_workflow import build_signal_report

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = build_signal_report(session)
        print(
            f"signals: total={report.total} awaiting_processing="
            f"{report.promoted_awaiting_processing} errors={report.processed_with_errors}"
        )
        if report.by_status:
            print("by status: " + ", ".join(f"{s}={n}" for s, n in sorted(report.by_status.items())))
        if report.by_type:
            print("by type: " + ", ".join(f"{t}={n}" for t, n in sorted(report.by_type.items())))
        for item in report.recent_refreshed:
            print(
                f"  refreshed #{item.signal_id} {item.market_ticker} ({item.signal_type}) "
                f"forecast={item.refreshed_forecast_id} p={item.refreshed_probability:.2f} "
                f"conf={item.refreshed_confidence:.2f}"
            )
        return report.total
    finally:
        if owns_session:
            session.close()


async def research_canary_report(session=None) -> int:
    """Print external-research canary metrics: packets by collector, domain,
    completeness, evidence depth, and fallback counts. Returns total packets."""
    from app.services.baseball_research import build_research_canary_report

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = build_research_canary_report(session)
        print(
            f"research canary: packets={report.total_packets} "
            f"external_fallbacks={report.external_fallbacks}"
        )
        for name, stats in sorted(report.by_collector.items()):
            depths = ", ".join(f"{d}={n}" for d, n in sorted(stats.by_evidence_depth.items()))
            print(
                f"  collector={name:<24} n={stats.count:<4} "
                f"mean_completeness={stats.mean_completeness} depths: {depths}"
            )
        if report.by_domain:
            print("by domain: " + ", ".join(f"{d}={n}" for d, n in sorted(report.by_domain.items())))
        if report.forecasts_by_forecaster:
            print(
                "forecasts by forecaster: "
                + ", ".join(f"{f}={n}" for f, n in sorted(report.forecasts_by_forecaster.items()))
            )
        return report.total_packets
    finally:
        if owns_session:
            session.close()


async def crypto_scan_once(limit: int | None = None, services=None, session=None) -> int:
    """One read-only crypto discovery pass (Crypto Arena, CRYPTO-001).
    Manual invocation is always allowed — ENABLE_CRYPTO_SCOUT only gates
    loop/timer use. Returns 0 on an ok pass, 1 on error."""
    from app.services.crypto_scout import CryptoDiscoveryService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        service = services or CryptoDiscoveryService()
        run = await service.scan_once(session, limit=limit)
        print(
            f"crypto scan #{run.id}: {run.status} tokens={run.tokens_checked} "
            f"pairs={run.pairs_checked} ticks={run.ticks_recorded} "
            f"signals={run.signals_created} in {run.duration_ms}ms"
        )
        return 0 if run.status == "ok" else 1
    finally:
        if owns_session:
            session.close()


async def crypto_signals_recent(limit: int = 20, session=None) -> int:
    """List recent crypto signals, newest first. Returns the count printed."""
    from sqlalchemy import select

    from app.models import CryptoOpportunitySignal

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        rows = session.execute(
            select(CryptoOpportunitySignal)
            .order_by(CryptoOpportunitySignal.id.desc())
            .limit(limit)
        ).scalars().all()
        print(f"{len(rows)} crypto signal(s)")
        for row in rows:
            print(
                f"  #{row.id} [{row.signal_type}] {row.token_address[:12]}… "
                f"pair={(row.pair_address or 'n/a')[:12]} status={row.signal_status} "
                f"at {row.observed_at:%Y-%m-%d %H:%M} — {row.reason}"
            )
        return len(rows)
    finally:
        if owns_session:
            session.close()


async def crypto_report(session=None) -> int:
    """Print the aggregate crypto surveillance report. Returns total tokens."""
    from app.services.crypto_scout import CryptoReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = CryptoReportService().build(session)
        print(
            "crypto report: "
            + ", ".join(f"{name}={count}" for name, count in sorted(report.totals.items()))
        )
        from app.config import get_settings as _settings

        s = _settings()
        engine_mode = (
            ("provider-backed" if (s.enable_goplus_risk or s.enable_solana_tracker_risk)
             else "heuristic-only")
            if s.enable_crypto_risk_engine
            else "disabled"
        )
        print(f"risk engine: {engine_mode} (see crypto-risk-report for details)")
        if report.signals_by_type:
            print(
                "signals by type: "
                + ", ".join(f"{t}={n}" for t, n in sorted(report.signals_by_type.items()))
            )
        if report.signals_by_status:
            print(
                "signals by status: "
                + ", ".join(f"{s}={n}" for s, n in sorted(report.signals_by_status.items()))
            )
        if report.risk_by_level:
            print(
                "risk by level: "
                + ", ".join(f"{lvl}={n}" for lvl, n in sorted(report.risk_by_level.items()))
            )
        if report.latest_run:
            run = report.latest_run
            print(
                f"latest run: #{run.id} {run.status} tokens={run.tokens_checked} "
                f"pairs={run.pairs_checked} ticks={run.ticks_recorded} "
                f"signals={run.signals_created}"
            )
        for run in report.provider_errors:
            print(f"  provider error run #{run.id}: {run.error_type}: {run.error_message}")
        for token in report.recent_tokens:
            print(
                f"  token {token.symbol or '?'} ({token.token_address[:12]}…) "
                f"last seen {token.last_seen_at:%Y-%m-%d %H:%M}"
            )
        return report.totals.get("tokens", 0)
    finally:
        if owns_session:
            session.close()


async def edge_precheck(
    limit: int = 50,
    force_readonly: bool = False,
    forecast_ids: list[int] | None = None,
    marketops_run_id: int | None = None,
    latest_marketops_run: bool = False,
    recent_refreshed_signals: bool = False,
    service=None,
    session=None,
) -> int:
    """Create probability-gap MEASUREMENT snapshots. Targeted modes
    (forecast ids / MarketOps cycle / recently refreshed signals) measure
    exactly the fresh forecasts; the broad latest-N sweep remains for manual
    diagnostics. Refuses unless ENABLE_EDGE_PRECHECK=true or
    --force-readonly is passed (either way only measurement rows are
    created — no advice, no actions). Returns snapshots created."""
    from app.config import get_settings
    from app.services.edge_precheck import EdgePrecheckService, summarize_snapshots

    if not get_settings().enable_edge_precheck and not force_readonly:
        print(
            "ENABLE_EDGE_PRECHECK=false; pass --force-readonly for a one-off "
            "measurement pass (still read-only, measurement rows only)"
        )
        return 0

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        svc = service or EdgePrecheckService()
        if forecast_ids:
            snapshots = svc.create_for_forecast_ids(session, forecast_ids)
            mode = f"forecast ids {forecast_ids}"
        elif marketops_run_id is not None:
            snapshots = svc.create_for_marketops_run(session, run_id=marketops_run_id)
            mode = f"marketops run #{marketops_run_id}"
        elif latest_marketops_run:
            snapshots = svc.create_for_marketops_run(session)
            mode = "latest marketops run"
        elif recent_refreshed_signals:
            snapshots = svc.create_for_recent_refreshed_signals(session, limit=limit)
            mode = f"recent refreshed signals (limit {limit})"
        else:
            snapshots = svc.run_batch(session, limit=limit)
            mode = f"broad sweep (limit {limit}; diagnostic)"
        summary = summarize_snapshots(snapshots)
        print(
            f"measured {len(snapshots)} forecast gap(s) via {mode} — measurement only, "
            f"not advice (watchlist={summary['edge_prechecks_watchlist']} "
            f"candidate_labels={summary['edge_prechecks_candidate_labels']} "
            f"no_gap={summary['edge_prechecks_no_gap']} "
            f"invalid={summary['edge_prechecks_invalid']})"
        )
        for row in snapshots:
            gap = f"{row.probability_gap:+.4f}" if row.probability_gap is not None else "n/a"
            print(
                f"  {row.market_ticker[:40]:<40} gap={gap} status={row.status} "
                f"persist={row.persistence_count} "
                f"reasons={','.join(row.invalidation_reasons or []) or 'none'}"
            )
        return len(snapshots)
    finally:
        if owns_session:
            session.close()


async def edge_precheck_report(session=None) -> int:
    """Print the aggregate gap-measurement report. Returns total snapshots."""
    from app.services.edge_precheck import EdgePrecheckReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = EdgePrecheckReportService().build(session)
        print(f"edge precheck (measurement only): snapshots={report.total_snapshots}")
        if report.by_status:
            print(
                "by status: "
                + ", ".join(f"{s}={n}" for s, n in sorted(report.by_status.items()))
            )
        if report.by_forecaster:
            print(
                "by forecaster: "
                + ", ".join(f"{f}={n}" for f, n in sorted(report.by_forecaster.items()))
            )
        if report.by_domain:
            print(
                "by domain: "
                + ", ".join(f"{d}={n}" for d, n in sorted(report.by_domain.items()))
            )
        if report.by_market_type:
            print(
                "by market type: "
                + ", ".join(f"{t}={n}" for t, n in sorted(report.by_market_type.items()))
            )
        if report.mean_gap is not None:
            print(f"mean gap: {report.mean_gap:+.4f}  mean |gap|: {report.mean_abs_gap:.4f}")
        print(f"paper_candidate_later (review label, no behavior): {report.paper_candidate_later_count}")
        if report.invalidation_reason_counts:
            print(
                "invalidation reasons: "
                + ", ".join(
                    f"{r}={n}" for r, n in report.invalidation_reason_counts.items()
                )
            )
        for row in report.recent_largest_gaps[:5]:
            gap = f"{row.probability_gap:+.4f}" if row.probability_gap is not None else "n/a"
            print(f"  largest: {row.market_ticker[:40]} gap={gap} status={row.status}")
        return report.total_snapshots
    finally:
        if owns_session:
            session.close()


async def frontier_eval_report(
    hours: int = 24,
    domains: list[str] | None = None,
    include_crypto: bool = False,
    include_safety: bool = False,
    save_run: bool = False,
    session=None,
) -> int:
    """Print the frontier evaluation report (evaluation only — no EV, no
    trades, no positions; readiness labels never authorize live capital).
    Returns 0."""
    from datetime import datetime, timezone

    from app.services.frontier_eval import FrontierEvalService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        started_at = datetime.now(timezone.utc)
        service = FrontierEvalService()
        report = service.build(
            session,
            hours=hours,
            domains=domains,
            include_crypto=include_crypto,
            include_safety=include_safety,
        )

        def section(title: str) -> None:
            print(f"\n== {title} ==")

        print(f"frontier eval (window {report.window_hours}h"
              + (f", domains {','.join(report.domains)}" if report.domains else "")
              + ") — evaluation only, never advice")
        section("executive summary")
        print(report.executive_summary)
        section("readiness scorecard")
        print(f"label: {report.readiness['label']}")
        for reason in report.readiness["reasons"]:
            print(f"  - {reason}")
        print(f"  note: {report.readiness['note']}")
        section("signal quality")
        for key, value in report.signal_quality.items():
            print(f"  {key}: {value}")
        section("forecast quality")
        for key, value in report.forecast_quality.items():
            print(f"  {key}: {value}")
        section("edge-precheck quality")
        for key, value in report.edge_precheck_quality.items():
            print(f"  {key}: {value}")
        section("gap follow-through (market movement, not PnL)")
        print(f"  rows analyzed: {report.gap_follow_through['watchlist_rows_analyzed']}")
        for horizon, stats in report.gap_follow_through["horizons"].items():
            print(f"  {horizon}: {stats}")
        section("microstructure quality")
        for key, value in report.microstructure_quality.items():
            print(f"  {key}: {value}")
        if report.crypto_risk_quality is not None:
            section("crypto risk quality")
            for key, value in report.crypto_risk_quality.items():
                print(f"  {key}: {value}")
        section("latency quality")
        for key, value in report.latency_quality.items():
            print(f"  {key}: {value}")
        if report.safety_audit is not None:
            section("safety audit")
            print(f"  files_scanned: {report.safety_audit['files_scanned']}")
            print(f"  safety_ok: {report.safety_audit['safety_ok']}")
            for violation in report.safety_audit["violations"]:
                print(f"  VIOLATION: {violation}")
        section("recommended next action")
        print(report.recommended_next_action)

        if save_run:
            row = service.persist_run(session, report, started_at, hours)
            print(f"\nsaved eval run #{row.id}")
        return 0
    finally:
        if owns_session:
            session.close()


async def edge_cohort_report(hours: int = 24, session=None) -> int:
    """Print the edge-precheck cohort analysis (analysis only — no EV, no
    trades, no positions; cohort labels authorize nothing). Returns 0."""
    from app.services.edge_cohort import EdgeCohortReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = EdgeCohortReportService().build(session, hours=hours)

        def section(title: str) -> None:
            print(f"\n== {title} ==")

        print(
            f"edge cohort analysis (window {report.window_hours}h) — analysis only, "
            "never advice"
        )
        print(report.note)
        print(
            f"snapshots={report.total_snapshots} "
            f"follow_through_rows={report.follow_through_rows}"
        )

        section("overall follow-through (market movement, not PnL)")
        for horizon, stats in report.overall_follow_through.items():
            print(f"  {horizon}: {stats}")

        for dim, cohorts in report.dimensions.items():
            section(f"cohort: {dim}")
            for c in cohorts:
                ft = c["follow_through"]
                rates = " ".join(
                    f"{h}={ft[h]['moved_toward_rate']}" for h in ("5m", "15m", "30m", "60m")
                )
                print(
                    f"  {c['key']:<22} n={c['sample']:<4} wl={c['watchlist']:<3} "
                    f"cand={c['paper_candidate_later']:<2} inv={c['invalid']:<3} "
                    f"|gap|={c['mean_abs_gap']} conf={c['confidence_avg']} "
                    f"ft_n={c['follow_through_samples']:<3} toward[{rates}] "
                    f"-> {c['recommendation']}"
                )

        section("conservative recommendation (no trading, no paper trading)")
        print("  promising (observe more — signal only, authorizes nothing):")
        for item in report.promising or ["(none)"]:
            print(f"    + {item}")
        print("  observe more (promising or neutral — needs more data):")
        for item in report.observe_more or ["(none)"]:
            print(f"    ~ {item}")
        print("  deprioritize in future gating (weak / exclude_candidate):")
        for item in report.deprioritize or ["(none)"]:
            print(f"    - {item}")

        section("MVP-005B-design gate")
        print(f"  blocked: {report.mvp_005b_blocked}")
        print(f"  {report.mvp_005b_reason}")
        return 0
    finally:
        if owns_session:
            session.close()


async def edge_policy_report(hours: int = 24, session=None) -> int:
    """Print the edge shadow-policy analysis (read-only — simulates cohort
    filters over existing rows; no EV, no trades, no live-gate change,
    authorizes nothing). Returns 0."""
    from app.services.edge_policy import EdgePolicyReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = EdgePolicyReportService().build(session, hours=hours)

        def section(title: str) -> None:
            print(f"\n== {title} ==")

        print(
            f"edge shadow-policy analysis (window {report.window_hours}h) — "
            "read-only, never advice"
        )
        print(report.note)
        print(
            f"measurement population (watchlist+candidate): {report.population}  "
            f"settlement_available={report.settlement_available}"
        )

        for p in report.policies:
            ft = p["follow_through"]
            rates = " ".join(
                f"{h}={ft[h]['moved_toward_rate']}" for h in ("5m", "15m", "30m", "60m")
            )
            section(f"policy: {p['name']}")
            print(
                f"  included={p['included']} wl={p['watchlist']} "
                f"cand={p['paper_candidate_later']} no_gap={p['no_gap']} "
                f"invalid={p['invalid']} invalid_rate={p['invalid_rate']}"
            )
            print(
                f"  follow_samples={p['follow_samples']} "
                f"blended_toward={p['blended_toward_rate']} toward[{rates}]"
            )
            closures = " ".join(
                f"{h}={ft[h]['mean_gap_closure_pct']}" for h in ("5m", "15m", "30m", "60m")
            )
            print(f"  gap_closure[{closures}]")
            print(f"  market_type={p['market_type_dist']} domain={p['domain_dist']}")
            print(
                f"  gap_bucket={p['gap_bucket_dist']} confidence={p['confidence_dist']} "
                f"persistence={p['persistence_dist']}"
            )
            s = p["settlement"]
            print(
                f"  settlement: resolved_n={s['resolved_samples']} "
                f"forecast_brier={s['forecast_brier']} market_brier={s['market_midpoint_brier']} "
                f"delta={s['forecast_minus_market_brier']} "
                f"log_loss={s['forecast_log_loss']} beats_market={s['forecast_beats_market_rate']}"
            )
            print(f"  -> {p['recommendation']}")

        section("decision")
        print("  clears follow-through gate (n>=20 & moved-toward>=0.55 @30m/60m):")
        for item in report.any_clears_follow_gate or ["(none)"]:
            print(f"    + {item}")
        print("  improves meaningfully over baseline:")
        for item in report.any_improves_over_baseline or ["(none)"]:
            print(f"    + {item}")
        print("  preserves enough sample (n>=20):")
        for item in report.any_preserves_sample or ["(none)"]:
            print(f"    ~ {item}")
        print(f"  settlement vs follow-through: {report.settlement_disagreement}")

        section("MVP-005B-design gate")
        print(f"  blocked: {report.mvp_005b_blocked}")
        print(f"  {report.mvp_005b_reason}")
        return 0
    finally:
        if owns_session:
            session.close()


async def meme_scan_once(limit: int | None = None, service=None, session=None) -> int:
    """One read-only meme/news attention scan pass (MEME-NEWS-001). Scores the
    newest/boosted tokens and records catalysts — no EV, no trade, no advice.
    Returns tokens scored, or -1 on error."""
    from app.services.meme_scout import MemeScoutService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        service = service or MemeScoutService()
        run = await service.scan_once(session, limit=limit)
        print(
            f"meme scan run #{run.id}: {run.status} profiles={run.profiles_seen} "
            f"boosts={run.boosts_seen} scored={run.tokens_scored} "
            f"catalysts={run.catalysts_created} (attention/interest only — not advice)"
        )
        return run.tokens_scored
    except Exception as exc:  # pragma: no cover - defensive
        print(f"meme scan failed: {type(exc).__name__}: {exc}")
        return -1
    finally:
        if owns_session:
            session.close()


async def meme_scout_report(session=None) -> int:
    """Aggregate meme attention report (read-only). Returns total snapshots."""
    from app.services.meme_scout import MemeScoutReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = MemeScoutReportService().build(session)
        print(f"meme scout (attention/interest only — not advice): snapshots={report.total_snapshots}")
        print(report.note)
        print(f"runs={report.total_runs}  latest_run={report.latest_run}")
        print(
            f"attention p50={report.attention_p50} p90={report.attention_p90}  "
            f"provider_confidence_avg={report.provider_confidence_avg}"
        )
        print(f"by risk level: {report.by_risk_level}")
        print("top attention (interest signal, no action):")
        for row in report.top_attention:
            print(
                f"  {str(row['symbol'])[:12]:<12} {row['token']:<16} "
                f"attention={row['attention_score']} age_s={row['age_seconds']} "
                f"boost={row['boost_amount']} liq={row['liquidity_usd']} "
                f"risk={row['risk_level']} conf={row['provider_confidence']}"
            )
        return report.total_snapshots
    finally:
        if owns_session:
            session.close()


async def catalyst_report(session=None) -> int:
    """Aggregate catalyst-event report (read-only). Returns total events."""
    from app.services.meme_scout import CatalystReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = CatalystReportService().build(session)
        print(f"catalyst events (informational, never a trade trigger): total={report.total}")
        print(report.note)
        print(f"by type: {report.by_type}")
        print(f"by source: {report.by_source}  by subject: {report.by_subject_type}")
        print("recent:")
        for r in report.recent:
            print(f"  {r['type']:<16} src={r['source']} subj={r['subject']} mag={r['magnitude']}")
        return report.total
    finally:
        if owns_session:
            session.close()


async def domain_scout_report(session=None) -> int:
    """Read-only market-domain inventory + candidate canary priority
    (MEME-NEWS-001, Part C). Adds no forecaster, changes no live logic.
    Returns domains seen."""
    from app.services.domain_scout import DomainScoutService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = DomainScoutService().build(session, persist=True)
        print(
            f"domain scout (inventory + canary priority — never advice): "
            f"markets={report.markets_scanned} domains={len(report.domains)} run={report.run_id}"
        )
        print(report.note)
        print(
            f"{'domain':<16} {'mkts':>5} {'act':>5} {'2side':>6} {'liq_$c':>12} "
            f"{'clarity':>7} {'fcaster':>7} {'priority':>8}  data_source"
        )
        for d in report.domains:
            print(
                f"{d['domain']:<16} {d['market_count']:>5} {d['active_count']:>5} "
                f"{str(d['two_sided_rate']):>6} {d['liquidity_proxy_cents']:>12} "
                f"{str(d['resolution_clarity_proxy']):>7} "
                f"{('yes' if d['has_evidence_forecaster'] else 'NO'):>7} "
                f"{str(d['canary_priority']):>8}  {d['data_source_notes']}"
            )
        return len(report.domains)
    finally:
        if owns_session:
            session.close()


async def meme_news_run_once(
    scheduled: bool = False, limit: int | None = None, runner=None, session=None
) -> int:
    """One bounded read-only meme/news discovery cycle (MEME-NEWS-002). When
    invoked by the systemd timer (`--scheduled`) it refuses unless
    ENABLE_MEME_NEWS_SCOUT=true; manual invocation is always allowed. No EV,
    no trade, no advice. Returns tokens scored, or -1 on error."""
    from app.config import get_settings
    from app.services.meme_news import MemeNewsScoutRunner

    if scheduled and not get_settings().enable_meme_news_scout:
        print("ENABLE_MEME_NEWS_SCOUT=false; scheduled meme-news cycle skipped (set true in .env)")
        return 0

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        runner = runner or MemeNewsScoutRunner()
        run = await runner.run_cycle(session, limit=limit)
        if run is None:
            print("meme-news cycle: no run recorded")
            return -1
        print(
            f"meme-news run #{run.id}: {run.status} profiles={run.profiles_seen} "
            f"boosts={run.boosts_seen} scored={run.tokens_scored} "
            f"catalysts={run.catalysts_created}"
            + (f" error={run.error_type}" if run.status == "error" else "")
            + " (read-only discovery — not advice)"
        )
        return run.tokens_scored if run.status == "ok" else -1
    finally:
        if owns_session:
            session.close()


async def meme_news_report(hours: int = 24, session=None) -> int:
    """Windowed meme/news discovery report (read-only). Returns new-token count."""
    from app.services.meme_news import MemeNewsReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = MemeNewsReportService().build(session, hours=hours)
        print(f"meme-news report (window {r.window_hours}h) — read-only discovery, not advice")
        print(r.note)
        print(f"last_run={r.last_run}")
        print(
            f"runs={r.runs_in_window} (errors={r.error_runs_in_window})  "
            f"new_tokens={r.new_tokens}  catalysts={r.catalysts_in_window}"
        )
        print(
            f"attention p50={r.attention_p50} p90={r.attention_p90} max={r.attention_max}  "
            f"provider_confidence_avg={r.provider_confidence_avg}  "
            f"missing_holder_coverage={r.missing_holder_coverage}"
        )
        print(f"row counts: {r.row_counts}")
        print("top attention (interest signal, no action):")
        for t in r.top_attention:
            print(
                f"  {str(t['symbol'])[:12]:<12} {t['token']:<16} attention={t['attention_score']} "
                f"risk={t['risk_level']} boost={t['boost_amount']} conf={t['provider_confidence']}"
            )
        if r.high_risk_tokens:
            print("severe/high-risk tokens (avoid/flag for review — not a trade direction):")
            for t in r.high_risk_tokens:
                print(f"  {str(t['symbol'])[:12]:<12} {t['token']:<16} risk={t['risk_level']}")
        return r.new_tokens
    finally:
        if owns_session:
            session.close()


async def meme_news_alerts(hours: int = 6, session=None) -> int:
    """Derived notable-event report (read-only, local, informational — no push,
    no recommendation). Returns the number of alerts."""
    from app.services.meme_news import MemeNewsAlertService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        alerts = MemeNewsAlertService().evaluate(session, hours=hours)
        print(
            f"meme-news alerts (window {hours}h): {len(alerts)} notable event(s) — "
            "informational only, never a trade trigger"
        )
        for a in alerts:
            tok = f" {a.token}" if a.token else ""
            print(f"  [{a.severity}] {a.alert_type}{tok}: {a.detail}")
        return len(alerts)
    finally:
        if owns_session:
            session.close()


async def meme_mas_report(hours: int = 24, top: int = 10, session=None) -> int:
    """Read-only MEME-MAS diagnostic review (MEME-MAS-001). `review_priority`
    triages human-review attention only — never a trade signal. Returns tokens
    assessed."""
    from app.services.meme_mas import MemeMasReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = MemeMasReportService().build(session, hours=hours, top=top)
        print(f"meme-mas diagnostic report (window {r.window_hours}h) — read-only, not advice")
        print(r.note)
        print(f"tokens_assessed={r.tokens_assessed}  by_priority={r.by_priority}")
        print(
            f"missing_provider_coverage={r.missing_coverage_tokens}  "
            f"provider_coverage={r.provider_coverage}"
        )
        print(f"sub-score distributions: {r.subscore_distributions}")
        print("top diagnostic candidates by review_priority (human-review triage, not a trade signal):")
        for c in r.top_candidates:
            print(
                f"  {str(c['symbol'])[:10]:<10} {c['token']:<16} {c['review_priority']:<15} "
                f"review={c['review_score']} S={c['structure']} V={c['velocity']} "
                f"T={c['timing']} R={c['risk_penalty']} reasons={c['top_reasons']}"
            )
        if r.risk_rejects:
            print("risk rejects (flagged for avoid/review — never a trade direction):")
            for c in r.risk_rejects:
                print(f"  {str(c['symbol'])[:10]:<10} {c['token']:<16} risk_reasons={c['risk_reasons']}")
        return r.tokens_assessed
    finally:
        if owns_session:
            session.close()


async def meme_mas_assess(limit: int = 20, hours: int = 24, session=None) -> int:
    """Per-token MEME-MAS diagnostic traces for the top `limit` tokens by
    review_score (MEME-MAS-001, read-only). Returns tokens shown."""
    from app.services.meme_mas import MemeMasReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        results = MemeMasReportService().assess_all(session, hours=hours)
        results = sorted(results, key=lambda x: -x.review_score)[:limit]
        print(f"meme-mas assess (window {hours}h, top {limit}) — read-only diagnostic, not advice")
        for r in results:
            print(
                f"  {str(r.symbol)[:10]:<10} {r.token_address[:16]:<16} "
                f"{r.review_priority:<15} review={r.review_score}"
            )
            print(f"     scores={r.scores()}")
            print(f"     trace={r.reasoning_trace}")
            if r.risk_reasons:
                print(f"     risk_reasons={r.risk_reasons}")
            if r.missing_evidence:
                print(f"     missing_evidence={r.missing_evidence}")
        return len(results)
    finally:
        if owns_session:
            session.close()


async def meme_shadow_report(lookback_hours: int = 48, top: int = 8, session=None) -> int:
    """Read-only follow-through / calibration analysis of MEME-MAS review_priority
    labels (MEME-SHADOW-001). Market-movement MEASUREMENT — not PnL, not advice.
    Returns the number of anchors measured."""
    from app.services.meme_shadow import MemeShadowReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = MemeShadowReportService().build(session, lookback_hours=lookback_hours)
        print(f"meme-shadow follow-through (lookback {r.lookback_hours}h) — read-only measurement, not advice")
        print(r.note)
        print(f"anchors={r.anchors}  horizons={r.horizons}  horizon_coverage={r.horizon_coverage}")
        print(f"calibration_recommendation: {r.calibration_recommendation}")
        print("outcome by review_priority (does the label separate later behavior?):")
        for c in r.by_review_priority:
            print(
                f"  {c['cohort']:<16} n={c['samples']:<4} [{c['label']}] "
                f"survival={c['survival_rate']} rug_incidence={c['rug_incidence']} "
                f"price_mean(1h={c['price_change_mean'].get('1h')}, 24h={c['price_change_mean'].get('24h')}) "
                f"attn_persist_1h={c['attention_persistence_1h']}"
            )
        print("outcome by review_score bucket:")
        for c in r.by_review_score_bucket:
            print(f"  {c['cohort']:<8} n={c['samples']:<4} survival={c['survival_rate']} price_1h={c['price_change_mean'].get('1h')}")
        print("outcome by risk_penalty bucket:")
        for c in r.by_risk_penalty_bucket:
            print(f"  {c['cohort']:<8} n={c['samples']:<4} survival={c['survival_rate']} rug_incidence={c['rug_incidence']}")
        print("outcome by risk reason (top):")
        for c in r.by_risk_reason[:top]:
            print(f"  {c['cohort']:<28} n={c['samples']:<4} survival={c['survival_rate']} rug_incidence={c['rug_incidence']}")
        print("outcome by concentration bucket:")
        for c in r.by_concentration:
            print(f"  {c['cohort']:<28} n={c['samples']:<4} survival={c['survival_rate']} rug_incidence={c['rug_incidence']}")
        return r.anchors
    finally:
        if owns_session:
            session.close()


async def meme_mas_calibration_report(lookback_hours: int = 48, session=None) -> int:
    """Before(v1)/after(v2) calibration comparison of MEME-MAS review_priority
    labels using MEME-SHADOW follow-through (MEME-MAS-002, read-only). Shows the
    high_review share shrinking and per-priority survival separation. Not advice."""
    from app.services.meme_mas import PROFILE_V1, PROFILE_V2, REVIEW_PRIORITIES
    from app.services.meme_shadow import MemeShadowReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        rv1 = MemeShadowReportService(profile=PROFILE_V1).build(session, lookback_hours=lookback_hours)
        rv2 = MemeShadowReportService(profile=PROFILE_V2).build(session, lookback_hours=lookback_hours)

        def dist(r):
            return {c["cohort"]: (c["samples"], c["survival_rate"], c["rug_incidence"]) for c in r.by_review_priority}

        d1, d2 = dist(rv1), dist(rv2)
        print("meme-mas calibration before(v1)/after(v2) — read-only label calibration, not advice")
        print(rv2.note)
        print(f"anchors: v1={rv1.anchors} v2={rv2.anchors}  lookback={lookback_hours}h")
        hi1 = d1.get("high_review", (0, None, None))[0]
        hi2 = d2.get("high_review", (0, None, None))[0]
        sh1 = round(hi1 / rv1.anchors, 4) if rv1.anchors else None
        sh2 = round(hi2 / rv2.anchors, 4) if rv2.anchors else None
        print(f"high_review share: v1={sh1}  ->  v2={sh2}")
        print(f"calibration_recommendation: v1={rv1.calibration_recommendation}  ->  v2={rv2.calibration_recommendation}")
        print(f"{'priority':<16}{'v1_n':>7}{'v1_surv':>9}{'v1_rug':>8}   |{'v2_n':>7}{'v2_surv':>9}{'v2_rug':>8}")
        for p in REVIEW_PRIORITIES:
            a = d1.get(p, (0, None, None))
            b = d2.get(p, (0, None, None))
            print(
                f"  {p:<14}{a[0]:>7}{str(a[1]):>9}{str(a[2]):>8}   |{b[0]:>7}{str(b[1]):>9}{str(b[2]):>8}"
            )
        return rv2.anchors
    finally:
        if owns_session:
            session.close()


async def meme_mas_objectives_report(lookback_hours: int = 48, session=None) -> int:
    """Multi-objective calibration of MEME-MAS review_priority across separate
    axes (MEME-MAS-003, read-only) — momentum follow-through, survival quality,
    risk-adjusted movement, review-queue efficiency, and coverage quality — v1 vs
    v2. Market-movement MEASUREMENT, not PnL/EV/advice. Returns anchors."""
    from app.services.meme_mas import PROFILE_V1, PROFILE_V2, REVIEW_PRIORITIES
    from app.services.meme_shadow import MemeShadowObjectivesService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        o1 = MemeShadowObjectivesService(profile=PROFILE_V1).build(session, lookback_hours=lookback_hours)
        o2 = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=lookback_hours)

        def idx(section):
            return {r["priority"]: r for r in section}

        print("meme-mas objectives (multi-objective calibration) — read-only measurement, not advice")
        print(o2.note)
        print(
            f"lookback={lookback_hours}h  anchors: v1={o1.anchors} v2={o2.anchors}  "
            f"overall_momentum_positive_rate_1h: v1={o1.overall_momentum_positive_rate} v2={o2.overall_momentum_positive_rate}"
        )

        def section(title, s1, s2, cols):
            print(f"\n[{title}]  (v1 | v2 by review_priority)")
            m1, m2 = idx(s1), idx(s2)
            for p in REVIEW_PRIORITIES:
                a, b = m1.get(p), m2.get(p)
                if a is None and b is None:
                    continue
                def fmt(r):
                    return "  ".join(f"{c}={(r or {}).get(c)}" for c in cols)
                print(f"  {p:<16} v1[n={(a or {}).get('n', 0)} {fmt(a)}]  |  v2[n={(b or {}).get('n', 0)} {fmt(b)}]")

        section("1 momentum_followthrough (does the label predict positive movement?)",
                o1.momentum_followthrough, o2.momentum_followthrough,
                ["momentum_positive_rate_1h", "price_1h_median", "price_24h_median"])
        section("2 survival_quality (is the tier safer?)",
                o1.survival_quality, o2.survival_quality,
                ["survival_rate", "rug_incidence", "severe_end_rate"])
        section("3 risk_adjusted_movement (median move discounted by survival — a diagnostic, not a return)",
                o1.risk_adjusted_movement, o2.risk_adjusted_movement,
                ["risk_adjusted_1h", "median_price_1h", "survival_rate"])
        section("4 review_queue_efficiency (share of queue + momentum-positive lift vs overall)",
                o1.review_queue_efficiency, o2.review_queue_efficiency,
                ["share", "lift", "momentum_positive_rate_1h"])

        print("\n[5 coverage_quality]  (label-independent: does MISSING provider coverage predict worse outcomes?)")
        for k in ("covered", "missing"):
            c = o2.coverage_quality.get(k, {})
            print(
                f"  {k:<10} n={c.get('n')} survival={c.get('survival_rate')} "
                f"rug_incidence={c.get('rug_incidence')} momentum_positive_1h={c.get('momentum_positive_rate_1h')} "
                f"price_1h_median={c.get('price_1h_median')}"
            )
        return o2.anchors
    finally:
        if owns_session:
            session.close()


async def polymarket_scan_once(
    scheduled: bool = False,
    limit: int | None = None,
    orderbook_limit: int | None = None,
    category: int | None = None,
    active_only: bool = True,
    include_closed: bool = False,
    query: list[str] | None = None,
    targeted: bool = False,
    end_date_min: str | None = None,
    end_date_max: str | None = None,
    runner=None,
    session=None,
) -> int:
    """One bounded read-only Polymarket market-data scan (POLY-001, broadened by
    POLY-COVERAGE-001). Fetches the public Gamma market catalog (paginated) plus
    optional public-search queries + CLOB order books, and persists market/
    orderbook/domain-inventory snapshots. With `--scheduled` it refuses unless
    ENABLE_POLYMARKET_SCOUT=true; manual invocation is always allowed. Returns
    markets persisted, or -1 on error.

    `--targeted` derives search queries deterministically from already-persisted
    Kalshi active market titles (no LLM, no external taxonomy) so the two venues'
    catalogs overlap. Read-only observation throughout — no EV, no arbitrage
    label, no trade recommendation, no sizing, no orders, no wallets, no signing,
    no execution."""
    from app.config import get_settings
    from app.services.polymarket import PolymarketScoutRunner

    if scheduled and not get_settings().enable_polymarket_scout:
        print("ENABLE_POLYMARKET_SCOUT=false; scheduled polymarket cycle skipped (set true in .env)")
        return 0

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        runner = runner or PolymarketScoutRunner()
        run = await runner.run_cycle(
            session,
            limit=limit,
            orderbook_limit=orderbook_limit,
            tag_id=category,
            active_only=active_only,
            include_closed=include_closed,
            queries=list(query) if query else None,
            targeted=targeted,
            end_date_min=end_date_min,
            end_date_max=end_date_max,
        )
        if run is None:
            print("polymarket scan: no run recorded")
            return -1
        print(
            f"polymarket run #{run.id}: {run.status} markets={run.markets_seen} "
            f"orderbooks={run.orderbooks_fetched} (errors={run.orderbook_errors}) "
            f"domains={run.domains_seen}"
            + (f" error={run.error_type}" if run.status == "error" else "")
            + " (read-only market-data observation — not advice)"
        )
        print(
            f"  coverage: mode={run.scan_mode} pages={run.pages_fetched} "
            f"market_fetch_errors={run.market_fetch_errors} "
            f"duplicates_dropped={run.duplicates_dropped} queries={run.queries_used or []}"
        )
        return run.markets_persisted if run.status == "ok" else -1
    finally:
        if owns_session:
            session.close()


async def polymarket_coverage_report(top: int = 30, kalshi_limit: int = 4000, session=None) -> int:
    """Read-only Polymarket COVERAGE census (POLY-COVERAGE-001): per-domain and
    per-market-type supply on both venues, order-book coverage, and which domains
    have (or lack) the structural prerequisites for a cross-venue comparison to be
    ATTEMPTED. Coverage counts only — never arbitrage, EV, a trade candidate, a
    recommendation, sizing, orders, wallets, signing, or execution. Returns the
    Polymarket market count."""
    from app.services.polymarket_coverage import PolymarketCoverageReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = PolymarketCoverageReportService().build(session, top=top, kalshi_limit=kalshi_limit)
        print("polymarket coverage report — read-only supply census, not advice")
        print(r.note)
        print(
            f"polymarket_markets={r.polymarket_markets} active={r.polymarket_active}  "
            f"kalshi_markets={r.kalshi_markets}"
            + (f" (TRUNCATED at --kalshi-limit={kalshi_limit}; census undercounts Kalshi)"
               if r.kalshi_truncated else "")
            + f"  categories={r.categories}"
        )
        print(
            f"orderbook_enabled={r.orderbook_enabled} snapshots={r.orderbook_snapshots}  "
            f"two_sided_rate={r.two_sided_rate}  spread p50={r.spread_p50} p90={r.spread_p90}  "
            f"avg_book_depth={r.avg_book_depth}"
        )
        print(f"polymarket market types: {r.polymarket_market_types}")
        print(f"kalshi market types:     {r.kalshi_market_types}")
        print(f"overlap domains: {r.overlap_domains}")
        print(f"domains with comparable SUPPLY (a comparison could be attempted): {r.comparable_supply_domains}")
        print("domains with NO comparable supply:")
        for d in r.no_comparable_supply_domains:
            print(f"  {d['domain']:<12} reasons={d['reasons']}")
        print("per-domain coverage (counts only, never advice):")
        for d in r.domains:
            print(
                f"  {str(d['domain'])[:12]:<12} poly={d['polymarket_markets']:<4} "
                f"active={d['polymarket_active']:<4} two_sided={d['two_sided_rate']} "
                f"resolution={d['polymarket_with_resolution']:<4} yes_scale={d['polymarket_yes_scale']:<4} "
                f"ob_cov={d['orderbook_coverage_rate']} kalshi={d['kalshi_markets']:<5} "
                f"comparable_supply={d['comparable_supply']}"
            )
        print("top categories:")
        for c in r.top_categories[:10]:
            print(f"  {str(c['category'])[:44]:<44} markets={c['markets']}")
        return r.polymarket_markets
    finally:
        if owns_session:
            session.close()


async def polymarket_report(hours: int = 24, session=None) -> int:
    """Windowed read-only Polymarket market-data report (POLY-001). Returns the
    markets-seen count."""
    from app.services.polymarket import PolymarketReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = PolymarketReportService().build(session, hours=hours)
        print(f"polymarket report (window {r.window_hours}h) — read-only market data, not advice")
        print(r.note)
        print(f"last_run={r.last_run}")
        print(
            f"runs={r.runs_in_window} (errors={r.error_runs_in_window})  "
            f"markets_seen={r.markets_seen}  active={r.active_markets}  "
            f"categories={r.categories}"
        )
        print(
            f"two_sided={r.two_sided_markets} (rate={r.two_sided_rate})  "
            f"orderbook_enabled={r.orderbook_enabled_markets}  "
            f"orderbook_snapshots={r.orderbook_snapshots_in_window}  "
            f"provider_errors={r.provider_errors_in_window}"
        )
        print(
            f"spread p50={r.spread_p50} p90={r.spread_p90}  "
            f"avg_book_depth={r.avg_book_total_depth}  "
            f"avg_book_liquidity_proxy={r.avg_book_liquidity_proxy}"
        )
        print(f"row counts: {r.row_counts}")
        print("newest markets (interest signal, no action):")
        for m in r.newest_markets:
            print(f"  {m['market_id']:<10} {str(m['question'])[:48]:<48} cat={m['category']}")
        print("highest 24h volume:")
        for m in r.top_volume_markets:
            print(
                f"  {m['market_id']:<10} vol24h=${m['volume_24h_usd']} "
                f"liq=${m['liquidity_usd']} spread={m['spread']} two_sided={m['two_sided']}"
            )
        print("highest liquidity:")
        for m in r.top_liquidity_markets:
            print(f"  {m['market_id']:<10} liq=${m['liquidity_usd']} vol24h=${m['volume_24h_usd']}")
        print(f"cross-venue: {r.cross_venue_note}")
        return r.markets_seen
    finally:
        if owns_session:
            session.close()


async def polymarket_domain_report(session=None) -> int:
    """Read-only Polymarket per-domain/category inventory from the latest scan
    (POLY-001). Returns the number of domains."""
    from app.services.polymarket import PolymarketDomainReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = PolymarketDomainReportService().build(session)
        print("polymarket domain report — read-only inventory, not advice")
        print(r.note)
        print(f"last_run_id={r.last_run_id}  domains={r.total_domains}")
        print("per-domain inventory (coverage only, never advice):")
        for d in r.domains:
            print(
                f"  {str(d['domain'])[:28]:<28} markets={d['market_count']} "
                f"active={d['active_count']} two_sided={d['two_sided_count']} "
                f"(rate={d['two_sided_rate']}) ob_enabled={d['orderbook_enabled_count']} "
                f"liq=${d['total_liquidity_usd']} vol24h=${d['total_volume_24h_usd']} "
                f"avg_spread={d['avg_spread']}"
            )
        print(f"cross-venue: {r.cross_venue_note}")
        return r.total_domains
    finally:
        if owns_session:
            session.close()


async def cross_venue_match_once(
    kalshi_limit: int = 4000,
    polymarket_limit: int = 500,
    recent_hours: int | None = None,
    domain: str | None = None,
    market_type: str | None = None,
    session=None,
) -> int:
    """One read-only Kalshi<->Polymarket cross-venue matching/observation pass
    (POLY-002). Identifies comparable markets + measures observable differences.
    Returns candidates created, or -1 on error. No EV, arbitrage, orders, or
    execution.

    Kalshi rows are loaded most-recently-seen first (XVENUE-OPS-001) so the
    default run considers current markets, not stale rowid-order slices.
    `--recent-hours` drops markets not seen in that window; `--domain` /
    `--market-type` narrow the sample. These change WHICH persisted rows are
    considered, never how they are matched."""
    from app.services.cross_venue import CrossVenueMatchingService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        run = CrossVenueMatchingService().match_once(
            session, kalshi_limit=kalshi_limit, polymarket_limit=polymarket_limit,
            recent_hours=recent_hours, domain=domain, market_type=market_type,
        )
        print(
            f"cross-venue run #{run.id}: {run.status} kalshi={run.kalshi_markets_considered} "
            f"polymarket={run.polymarket_markets_considered} candidates={run.candidates_created} "
            f"comparable={run.comparable_count} unresolved={run.unresolved_count}"
            + (f" error={run.error_type}" if run.status == "error" else "")
            + " (read-only cross-venue observation — not advice, not arbitrage, not EV)"
        )
        s = getattr(run, "_sample", None)
        if s is not None:
            print(
                f"  sample: kalshi loaded={s.kalshi_loaded} considered={s.kalshi_considered} "
                f"(mode={s.kalshi_load_mode}"
                + (f", recent_hours={s.recent_hours}, stale_skipped={s.kalshi_stale_skipped}" if s.recent_hours else "")
                + (f", no_snapshot={s.kalshi_without_snapshot}" if s.kalshi_without_snapshot else "")
                + f")  polymarket loaded={s.polymarket_loaded} considered={s.polymarket_considered}"
            )
            if s.domain_filter or s.market_type_filter:
                print(f"  filters: domain={s.domain_filter} market_type={s.market_type_filter}")
            print(f"  kalshi by domain:     {s.kalshi_by_domain}")
            print(f"  polymarket by domain: {s.polymarket_by_domain}")
            print(f"  kalshi by market type:     {s.kalshi_by_market_type}")
            print(f"  polymarket by market type: {s.polymarket_by_market_type}")
            print(f"  domain overlap: {s.overlap_domains or '(none)'}")
            if s.low_overlap:
                print(
                    "  note: low sample overlap — no comparable rows surfaced. This is an "
                    "observation-coverage note, not a signal. Try a larger --polymarket-limit/"
                    "--kalshi-limit, drop --recent-hours, or scan more Polymarket supply "
                    "(polymarket-scan-once --targeted)."
                )
        return run.candidates_created if run.status == "ok" else -1
    finally:
        if owns_session:
            session.close()


async def xvenue_observation_report(top: int = 10, session=None) -> int:
    """Read-only cross-venue observation-window report (XVENUE-OBS-001): did the
    latest targeted Polymarket scan + match pass produce CLEAN comparable markets,
    and if not, why not? Composes persisted rows only — no external call, no
    persistence. Coverage intelligence for human review; never arbitrage, EV, a
    trade candidate, a recommendation, sizing, orders, wallets, signing, or
    execution. Returns the candidate count (0 is a valid result), or -1 on error."""
    from app.services.xvenue_observation import XVenueObservationReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = XVenueObservationReportService().build(session, top=top)
        print("cross-venue observation window report — read-only coverage, not advice")
        print(r.note)
        print(
            f"scan: run #{r.scan_run_id} started={r.scan_started_at} mode={r.scan_mode} "
            f"markets={r.scan_markets_seen} queries={r.scan_queries}"
        )
        print(
            f"match: run #{r.match_run_id} started={r.match_started_at} "
            f"ran_after_scan={r.match_ran_after_scan} kalshi={r.kalshi_considered} "
            f"polymarket={r.polymarket_considered}"
        )
        print(
            f"candidates={r.candidates}  comparable: total={r.comparable_total} "
            f"clean={r.comparable_clean} flagged_for_review={r.comparable_flagged}  "
            f"side_uncertain={r.side_uncertain}  unresolved={r.unresolved}"
        )
        print(f"by label:  {r.by_label}")
        print(f"by domain: {r.by_domain}")
        print(f"mismatch reasons: {r.mismatch_reasons}")
        if r.clean_candidates:
            print("clean comparable candidates (observation only — never a trade/arb signal):")
            for c in r.clean_candidates:
                print(
                    f"  {str(c['kalshi_ticker'])[:24]:<24} <-> {str(c['polymarket_market_id'])[:10]:<10} "
                    f"[{c['domain']}] conf={c['match_confidence']} "
                    f"k_mid={c['kalshi_midpoint']} p_mid={c['polymarket_midpoint']} "
                    f"observed_diff={c['observed_difference']}"
                )
        if r.flagged_candidates:
            print("comparable rows FLAGGED for review (suspicious match / stale quote — not opportunities):")
            for c in r.flagged_candidates:
                print(
                    f"  {str(c['kalshi_ticker'])[:24]:<24} <-> {str(c['polymarket_market_id'])[:10]:<10} "
                    f"conf={c['match_confidence']} observed_diff={c['observed_difference']}"
                )
        print(f"overlap assessment: {r.overlap_assessment}")
        print(f"  {r.assessment_detail}")
        return r.candidates
    finally:
        if owns_session:
            session.close()


async def cross_venue_report(session=None) -> int:
    """Read-only cross-venue observation report (POLY-002). Returns candidate count."""
    from app.services.cross_venue import CrossVenueReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = CrossVenueReportService().build(session)
        print("cross-venue observation report — read-only, not advice")
        print(r.note)
        print(f"last_run={r.last_run}")
        print(f"candidates={r.candidates}  by_label={r.by_label}  by_domain={r.by_domain}")
        print(
            f"midpoint_difference (|kalshi_mid - polymarket_mid|, probability points, NOT EV/arb): "
            f"n={r.midpoint_difference.get('n')} p50={r.midpoint_difference.get('abs_p50')} "
            f"p90={r.midpoint_difference.get('abs_p90')} max={r.midpoint_difference.get('abs_max')}"
        )
        print(
            f"spread comparison: kalshi_p50={r.spread_liquidity.get('kalshi_spread_p50')} "
            f"polymarket_p50={r.spread_liquidity.get('polymarket_spread_p50')}"
        )
        print(
            f"freshness/coverage: observation_confidence p50={r.freshness.get('observation_confidence_p50')} "
            f"p90={r.freshness.get('observation_confidence_p90')}"
        )
        print(f"mismatch reasons: {r.mismatch_reasons}")
        print("high-confidence comparable markets (observation only — never a trade/arb signal):")
        for c in r.comparable:
            print(
                f"  {str(c['kalshi_ticker'])[:22]:<22} <-> {str(c['polymarket_market_id'])[:12]:<12} "
                f"[{c['domain']}] conf={c['match_confidence']} k_mid={c['kalshi_midpoint']} "
                f"p_mid={c['polymarket_midpoint']} observed_diff={c['observed_difference']}"
            )
        if r.unresolved:
            print("unresolved semantic matches (ambiguous — for human review):")
            for c in r.unresolved:
                print(f"  {str(c['kalshi_ticker'])[:22]:<22} <-> {str(c['polymarket_market_id'])[:12]:<12} conf={c['match_confidence']}")
        print(f"row counts: {r.row_counts}")
        return r.candidates
    finally:
        if owns_session:
            session.close()


async def cross_venue_candidates(label: str | None = None, session=None) -> int:
    """List cross-venue candidates from the latest run (POLY-002, read-only),
    optionally filtered by match_label. Returns count listed."""
    from app.services.cross_venue import CrossVenueMatchingService  # noqa: F401 (ensures models registered)
    from app.models import CrossVenueMarketCandidate, CrossVenueObservationRun
    from sqlalchemy import select

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        last = session.execute(
            select(CrossVenueObservationRun).where(CrossVenueObservationRun.status == "ok")
            .order_by(CrossVenueObservationRun.id.desc())
        ).scalars().first()
        if last is None:
            print("cross-venue candidates: no completed run yet")
            return 0
        q = select(CrossVenueMarketCandidate).where(CrossVenueMarketCandidate.run_id == last.id)
        if label:
            q = q.where(CrossVenueMarketCandidate.match_label == label)
        rows = session.execute(q.order_by(CrossVenueMarketCandidate.match_confidence.desc())).scalars().all()
        print(f"cross-venue candidates (run #{last.id}{', label=' + label if label else ''}) — read-only observation, not advice")
        for c in rows:
            print(
                f"  [{c.match_label}] {str(c.kalshi_ticker)[:22]:<22} <-> {str(c.polymarket_market_id)[:12]:<12} "
                f"conf={c.match_confidence} observed_diff={c.observed_difference} "
                f"reasons={(c.match_reasons or [])[:3]}"
            )
        return len(rows)
    finally:
        if owns_session:
            session.close()


async def backup_db() -> int:
    """Create a compressed, timestamped SQLite backup (consistent snapshot
    via the online backup API) and prune old ones. Returns 0 on success."""
    from app.services.backup import BackupResult, backup_database

    result = backup_database()
    if isinstance(result, str):
        print(result)  # non-SQLite guidance; nothing executed
        return 1
    assert isinstance(result, BackupResult)
    print(f"backup written: {result.path} ({result.size_bytes / (1024 * 1024):.2f} MiB)")
    for name in result.pruned:
        print(f"  pruned old backup: {name}")
    return 0


async def list_db_backups() -> int:
    """List existing backups, newest first. Returns the count."""
    from app.services.backup import list_backups

    backups = list_backups()
    print(f"{len(backups)} backup(s)")
    for path in backups:
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f"  {path.name}  {size_mb:.2f} MiB")
    return len(backups)


async def verify_db_backup(path: str) -> int:
    """Verify a backup is readable, passes integrity_check, and contains the
    expected core tables. Returns 0 when ok."""
    from app.services.backup import verify_backup

    result = verify_backup(path)
    print(f"{'OK' if result.ok else 'FAILED'}: {result.detail}")
    return 0 if result.ok else 1


async def crypto_risk_assess(limit: int = 50, engine=None, session=None) -> int:
    """Assess risk for recently-seen tokens from persisted Crypto Arena data
    (heuristics always; providers when their flags are on). Read-only risk
    intelligence — a score is an avoid/flag verdict, never a trade
    recommendation. Returns the number of tokens assessed."""
    from sqlalchemy import select

    from app.models import CryptoPair, CryptoPriceTick, CryptoToken
    from app.services.crypto_risk_engine import RISK_SIGNAL_TYPES, CryptoRiskEngine
    from app.services.crypto_scout import CryptoSignalService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        engine = engine or CryptoRiskEngine()
        signal_service = CryptoSignalService()
        tokens = session.execute(
            select(CryptoToken)
            .order_by(CryptoToken.last_seen_at.desc(), CryptoToken.id.desc())
            .limit(limit)
        ).scalars().all()
        assessed = 0
        risk_signals = 0
        for token in tokens:
            pairs = session.execute(
                select(CryptoPair).where(
                    CryptoPair.base_token_address == token.token_address
                )
            ).scalars().all()
            best_pair = None
            latest = previous = None
            best_liquidity = -1.0
            for pair in pairs:
                ticks = session.execute(
                    select(CryptoPriceTick)
                    .where(CryptoPriceTick.pair_address == pair.pair_address)
                    .order_by(CryptoPriceTick.observed_at.desc(), CryptoPriceTick.id.desc())
                    .limit(2)
                ).scalars().all()
                if ticks and (ticks[0].liquidity_usd or 0) > best_liquidity:
                    best_liquidity = ticks[0].liquidity_usd or 0
                    best_pair = pair
                    latest = ticks[0]
                    previous = ticks[1] if len(ticks) > 1 else None
            evaluation = await engine.evaluate(
                session,
                token=token,
                pair=best_pair,
                tick=latest,
                previous=previous,
                pair_count=len(pairs),
            )
            assessed += 1
            # Risk-type signals only (market detectors belong to scans)
            if best_pair is not None and latest is not None:
                from datetime import datetime, timezone

                now = datetime.now(timezone.utc)
                detected = [
                    signal
                    for signal in signal_service.detect(
                        best_pair, previous, latest, evaluation.as_signal_view(), now
                    )
                    if signal.signal_type in RISK_SIGNAL_TYPES
                ]
                risk_signals += signal_service.persist_deduped(session, detected, now)
            print(
                f"  {token.symbol or '?':<12} {token.token_address[:12]}… "
                f"level={evaluation.composite_risk_level:<8} "
                f"score={evaluation.composite_risk_score} "
                f"reasons={','.join(evaluation.reasons) or 'none'}"
            )
        session.commit()
        print(f"assessed {assessed} token(s), created {risk_signals} risk signal(s)")
        return assessed
    finally:
        if owns_session:
            session.close()


async def crypto_risk_report(session=None) -> int:
    """Print the aggregate crypto risk report. Returns tokens assessed."""
    from app.services.crypto_risk_engine import CryptoRiskReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = CryptoRiskReportService().build(session)
        print(
            f"crypto risk: engine={report.engine_mode} (heuristics {report.engine_version}) "
            f"providers={','.join(report.enabled_providers) or 'none'}"
        )
        print(
            f"assessments={report.assessments_total} tokens_assessed={report.tokens_assessed}"
        )
        if report.by_level:
            print(
                "by level: " + ", ".join(f"{lvl}={n}" for lvl, n in sorted(report.by_level.items()))
            )
        if report.common_reasons:
            print(
                "common reasons: "
                + ", ".join(f"{r}={n}" for r, n in report.common_reasons.items())
            )
        if report.risk_signals_created:
            print(
                "risk signals created: "
                + ", ".join(f"{t}={n}" for t, n in sorted(report.risk_signals_created.items()))
            )
        if report.provider_use:
            print(
                "provider use: "
                + ", ".join(f"{p}={n}" for p, n in sorted(report.provider_use.items()))
            )
        if report.provider_error_counts:
            print(
                "provider errors: "
                + ", ".join(f"{p}={n}" for p, n in sorted(report.provider_error_counts.items()))
            )
        for row in report.top_risky_tokens:
            print(
                f"  RISKY {row.token_address[:16]}… level={row.composite_risk_level} "
                f"score={row.composite_risk_score} reasons={','.join(row.risk_reasons or [])}"
            )
        # MEME-RISK-003 holder-risk coverage overlay (explicit absence)
        from app.services.crypto_provider_health import (
            HOLDER_RISK_DIMENSIONS,
            CryptoProviderHealthReportService,
        )

        health = CryptoProviderHealthReportService().build(session)
        cov = " ".join(
            f"{d}={health.observed_coverage[d]['rate']}" for d in HOLDER_RISK_DIMENSIONS
        )
        print(f"holder-risk coverage (rate of assessments with data): {cov}")
        if health.coverage_gaps:
            print(
                "  COVERAGE GAP — no active provider for: "
                + ", ".join(health.coverage_gaps)
                + " (see crypto-provider-health-report)"
            )
        return report.tokens_assessed
    finally:
        if owns_session:
            session.close()


async def crypto_provider_health_report(session=None) -> int:
    """Print crypto provider coverage/health (MEME-RISK-003, read-only). Makes
    provider absence explicit. Returns 0."""
    from app.services.crypto_provider_health import CryptoProviderHealthReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = CryptoProviderHealthReportService().build(session)
        print(f"crypto provider health — engine={r.engine_mode} (read-only risk intelligence)")
        print(r.note)
        print("providers:")
        for p in r.providers:
            print(
                f"  {p['name']:<15} status={p['status']:<9} enabled={p['enabled']} "
                f"key_present={p['key_present']} covers={','.join(p['dimensions']) or '-'}"
            )
        print(f"covered dimensions (active providers): {r.covered_dimensions}")
        print(
            "COVERAGE GAPS (no active provider): "
            + (", ".join(r.coverage_gaps) if r.coverage_gaps else "(none)")
        )
        print("observed coverage over recent assessments:")
        for dim, o in r.observed_coverage.items():
            print(f"  {dim:<14} {o['covered']}/{o['total']} rate={o['rate']}")
        print(f"provider use: {r.provider_use}  errors: {r.provider_error_counts}")
        return 0
    finally:
        if owns_session:
            session.close()


async def crypto_provider_budget_report(session=None) -> int:
    """Print SolanaTracker request accounting + budget status (PROVIDER-BUDGET-001,
    read-only cost/usage observability). Returns 0."""
    from app.services.provider_budget import SolanaTrackerBudgetService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = SolanaTrackerBudgetService().status(session)
        print(f"SolanaTracker budget — {r.plan_name} ({r.monthly_cost_usd}/month) — read-only cost/usage")
        print(r.note)
        print(
            f"enabled={r.provider_enabled}  monthly_limit={r.monthly_request_limit:,}  "
            f"daily_budget={r.daily_budget:,}  hourly_budget={r.hourly_budget}  "
            f"per_run_lookup_limit={r.per_run_lookup_limit}  cache_ttl_hours={r.cache_ttl_hours}"
        )
        print(
            f"requests: hour={r.requests_this_hour} today={r.requests_today} "
            f"month={r.requests_this_month} rolling_24h={r.rolling_24h_requests}"
        )
        print(
            f"estimated monthly run-rate={r.estimated_monthly_run_rate:,}  "
            f"remaining_daily={r.remaining_daily_budget:,}  remaining_monthly={r.remaining_monthly_budget:,}"
        )
        print(
            f"thresholds: warn_daily={r.warn_daily:,} (over={r.over_warn})  "
            f"stop_daily={r.stop_daily:,} (over={r.over_stop})  "
            f"hourly (over={r.over_hourly})"
        )
        print(
            f"success={r.success_count} error={r.error_count} success_rate={r.success_rate}  "
            f"coverage_per_request={r.coverage_per_request}"
        )
        print(f"recommendation: {r.recommendation}")
        return 0
    finally:
        if owns_session:
            session.close()


async def meme_risk_coverage_report(hours: int = 24, session=None) -> int:
    """Print holder-risk coverage for the meme-news lane (MEME-RISK-003,
    read-only). Returns tokens covered in the window."""
    from app.services.crypto_provider_health import MemeRiskCoverageReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        r = MemeRiskCoverageReportService().build(session, hours=hours)
        print(f"meme-risk coverage (window {r.window_hours}h) — read-only risk intelligence")
        print(r.note)
        print(
            f"tokens={r.tokens}  with_provider_data={r.with_provider_data}  "
            f"missing_provider_data={r.missing_provider_data}  provider_use={r.provider_use}"
        )
        print("holder-risk dimension coverage:")
        for dim, o in r.by_dimension.items():
            print(f"  {dim:<14} {o['covered']}/{r.tokens} rate={o['rate']}")
        print(
            "COVERAGE GAPS (no token had this dimension): "
            + (", ".join(r.coverage_gaps) if r.coverage_gaps else "(none)")
        )
        return r.tokens
    finally:
        if owns_session:
            session.close()


async def marketops_run_once(services=None, session=None) -> int:
    """One MarketOps Autopilot cycle (read-only coordination of existing
    services). Manual invocation is always allowed — ENABLE_MARKETOPS_AUTOPILOT
    only gates the loop/timer. Returns 0 for ok/partial, 1 for error."""
    from app.services.marketops import MarketOpsAutopilotService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        service = services or MarketOpsAutopilotService()
        run = await service.run_once(session)
        if run.status == "skipped":
            reason = (run.summary or {}).get("reason", "already_running")
            active_id = (run.summary or {}).get("active_run_id")
            print(
                f"marketops run #{run.id}: skipped ({reason}, active run "
                f"#{active_id}) — try again after the current cycle finishes"
            )
            return 0
        print(
            f"marketops run #{run.id}: {run.status} "
            f"signals seen={run.signals_seen} promoted={run.signals_promoted} "
            f"processed={run.signals_processed} crypto tokens={run.crypto_tokens_seen} "
            f"crypto signals={run.crypto_signals_created} synced={run.outcomes_synced} "
            f"scored={run.forecasts_scored} alerts={run.alerts_created} "
            f"in {run.duration_ms}ms"
        )
        stage_errors = (run.summary or {}).get("stage_errors") or {}
        for name, error in stage_errors.items():
            print(f"  stage {name}: {error}")
        if run.error_type:
            print(f"  run error: {run.error_type}: {run.error_message}")
        return 0 if run.status in ("ok", "partial") else 1
    finally:
        if owns_session:
            session.close()


async def marketops_report(session=None) -> int:
    """Print the aggregate MarketOps report. Returns total runs."""
    from app.services.marketops import MarketOpsReportService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        report = MarketOpsReportService().build(session)
        if report.latest_run:
            run = report.latest_run
            print(
                f"last run: #{run.id} {run.status} at {run.started_at:%Y-%m-%d %H:%M} — "
                f"signals seen={run.signals_seen} promoted={run.signals_promoted} "
                f"processed={run.signals_processed}, crypto tokens={run.crypto_tokens_seen} "
                f"signals={run.crypto_signals_created}, synced={run.outcomes_synced} "
                f"scored={run.forecasts_scored}"
            )
        else:
            print("last run: none")
        promo = (
            (report.latest_run.summary or {}).get("promotion")
            if report.latest_run
            else None
        )
        if promo:
            print(
                f"promotion (OPS-009): age mean={promo.get('promoted_signal_age_s_mean')}s "
                f"max={promo.get('promoted_signal_age_s_max')}s "
                f"skipped_stale={promo.get('skipped_stale_count')} "
                f"unmeasurable={promo.get('unmeasurable_candidates')}"
            )
            if promo.get("promoted_by_domain"):
                print(f"  promoted by domain: {promo['promoted_by_domain']}")
            if promo.get("promoted_by_market_type"):
                print(f"  promoted by market type: {promo['promoted_by_market_type']}")
        print(f"runs total: {report.runs_total}")
        print(f"source-backed packets: {report.source_backed_packets}")
        if report.forecasts_by_forecaster:
            print(
                "forecasts by forecaster: "
                + ", ".join(f"{f}={n}" for f, n in sorted(report.forecasts_by_forecaster.items()))
            )
        if report.champion_challenger:
            cc = report.champion_challenger
            print(
                f"champion/challenger: pairs={cc.get('pair_count')} "
                f"({cc.get('sample_label')}) mean_delta_brier={cc.get('mean_delta_brier')}"
            )
        print(
            "crypto totals: "
            + ", ".join(f"{k}={v}" for k, v in sorted(report.crypto_totals.items()))
        )
        if report.database_size_mb is not None:
            print(f"db size: {report.database_size_mb} MiB")
        print(f"open alerts: {len(report.open_alerts)}")
        for alert in report.open_alerts:
            print(f"  #{alert.id} [{alert.severity}] {alert.alert_type}: {alert.title}")
        print(f"recommended action: {report.recommended_action}")
        return report.runs_total
    finally:
        if owns_session:
            session.close()


async def marketops_alerts(limit: int = 20, alert_status: str | None = None, session=None) -> int:
    """List recent MarketOps alerts, newest first. Returns the count printed."""
    from app.services.marketops import MarketOpsAlertService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        rows = MarketOpsAlertService().list_recent(session, limit=limit, status=alert_status)
        print(f"{len(rows)} alert(s)")
        for alert in rows:
            resolved = f" resolved={alert.resolved_at:%Y-%m-%d %H:%M}" if alert.resolved_at else ""
            print(
                f"  #{alert.id} [{alert.severity}] {alert.alert_type} ({alert.status}) "
                f"{alert.title} — {alert.message[:120]}{resolved}"
            )
        return len(rows)
    finally:
        if owns_session:
            session.close()


async def marketops_resolve_alert(alert_id: int, session=None) -> int:
    """Resolve one alert by id. Returns 0 on success, 1 when not found."""
    from app.services.marketops import MarketOpsAlertService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        try:
            alert = MarketOpsAlertService().resolve(session, alert_id)
        except LookupError as exc:
            print(str(exc))
            return 1
        print(f"alert #{alert.id} resolved ({alert.alert_type}: {alert.title})")
        return 0
    finally:
        if owns_session:
            session.close()


async def marketops_loop(
    interval: int | None = None,
    services=None,
    session=None,
    max_iterations: int | None = None,
) -> int:
    """Run autopilot cycles on an interval until SIGINT/SIGTERM (or
    max_iterations, for tests). Requires ENABLE_MARKETOPS_AUTOPILOT=true.
    Per-cycle errors are printed and the loop continues. Returns iterations."""
    import asyncio as aio
    import signal as os_signal

    from app.config import get_settings
    from app.services.marketops import MarketOpsAutopilotService

    settings = get_settings()
    if not settings.enable_marketops_autopilot:
        print("ENABLE_MARKETOPS_AUTOPILOT=false; set it to true in .env to run the loop")
        return 0
    if interval is None:
        interval = settings.marketops_loop_interval_seconds

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()

    stop = aio.Event()
    loop = aio.get_running_loop()
    for sig in (os_signal.SIGINT, os_signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    service = services or MarketOpsAutopilotService()
    iterations = 0
    print(f"marketops loop started (interval={interval}s); Ctrl-C to stop")
    while not stop.is_set():
        if owns_session:
            from app.db import get_sessionmaker

            iteration_session = get_sessionmaker()()
        else:
            iteration_session = session
        try:
            run = await service.run_once(iteration_session)
            print(
                f"marketops run={run.id} status={run.status} "
                f"promoted={run.signals_promoted} processed={run.signals_processed} "
                f"alerts={run.alerts_created}"
            )
        except Exception as exc:
            print(f"marketops cycle failed: {type(exc).__name__}: {exc}")
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
    print(f"marketops loop stopped after {iterations} iteration(s)")
    return iterations


async def agent_context() -> int:
    """Print the project canon for coding/ops agents: phase, state, flags,
    allowed/forbidden capabilities, and where the docs live. Read-only —
    runs no migrations and mutates nothing. Returns 0."""
    import subprocess
    from pathlib import Path

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine.url import make_url

    from app import canon
    from app.config import get_settings

    repo_root = Path(__file__).resolve().parents[1]
    settings = get_settings()

    print(f"project: {canon.PROJECT_NAME}")
    print(f"phase:   {canon.CURRENT_PHASE}")

    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        print(f"commit:  {commit or 'unavailable'}")
    except Exception:
        print("commit:  unavailable")

    url = make_url(settings.database_url)
    print(f"database: {url.render_as_string(hide_password=True)}")
    try:
        connect_args = {} if url.get_backend_name() == "sqlite" else {"connect_timeout": 3}
        engine = create_engine(settings.database_url, connect_args=connect_args)
        with engine.connect() as conn:
            revision = conn.execute(text("select version_num from alembic_version")).scalar()
        engine.dispose()
        print(f"alembic revision: {revision}")
    except Exception:
        print("alembic revision: unavailable (database not initialized or unreachable)")

    print("feature flags:")
    for flag in canon.KEY_FEATURE_FLAGS:
        print(f"  {flag.upper():<42} {getattr(settings, flag, 'n/a')}")

    print("allowed capabilities:")
    for capability in canon.ALLOWED_CAPABILITIES:
        print(f"  + {capability}")
    print("forbidden capabilities (see docs/SAFETY_BOUNDARIES.md):")
    for capability in canon.FORBIDDEN_CAPABILITIES:
        print(f"  - {capability}")

    print("expected services (EVO-X2):")
    for service in canon.EXPECTED_SERVICES_EVO_X2:
        print(f"  * {service}")
    print("safe next milestones:")
    for milestone in canon.NEXT_MILESTONES:
        print(f"  > {milestone}")
    print("canon docs (read AGENTS.md first):")
    for doc in canon.CANON_DOCS:
        print(f"  {repo_root / doc}")
    return 0


async def champion_challenger_report(
    baseline: str = "template_baseline",
    challenger: str = "baseball_evidence_v1",
    domain: str | None = None,
    paired_only: bool = False,
    min_count: int = 30,
    session=None,
) -> int:
    """Print the champion/challenger comparison. Returns the smaller side's
    scored count (the effective sample size)."""
    from app.services.champion_challenger import ChampionChallengerService

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker, run_migrations

        run_migrations()
        session = get_sessionmaker()()
    try:
        summary = ChampionChallengerService().compare(
            session,
            baseline=baseline,
            challenger=challenger,
            domain=domain,
            paired_only=paired_only,
            min_count=min_count,
        )

        def metric_line(side):
            m = side.scored
            return (
                f"n={m.count_scored:<4} brier={m.mean_brier} log_loss={m.mean_log_loss} "
                f"abs_err={m.mean_absolute_error} (coverage={side.coverage}, "
                f"pending={side.pending}, unscorable={side.unscorable})"
            )

        print(
            f"champion/challenger: {summary.baseline_forecaster} vs "
            f"{summary.challenger_forecaster} [{summary.comparison_basis}] "
            f"sample={summary.sample_label}"
        )
        if summary.filters:
            print(f"filters: {summary.filters}")
        print(f"  baseline   {metric_line(summary.baseline)}")
        print(f"  challenger {metric_line(summary.challenger)}")
        print(
            f"  deltas (challenger-baseline; <0 favors challenger): "
            f"brier={summary.delta_brier} log_loss={summary.delta_log_loss} "
            f"abs_err={summary.delta_absolute_error}"
        )
        if summary.paired:
            p = summary.paired
            print(
                f"  PAIRED (same market+outcome): pairs={p.pair_count} "
                f"wins={p.wins} losses={p.losses} ties={p.ties} "
                f"win_rate={p.win_rate_by_market} "
                f"d_brier={p.mean_delta_brier} d_log_loss={p.mean_delta_log_loss} "
                f"[{p.sample_label}]"
            )
        else:
            print("  PAIRED: no same-market pairs yet — unpaired aggregates only (less reliable)")
        if summary.warning:
            print(f"  !! WARNING: {summary.warning}")

        for title, cohorts in (
            ("by market_type", summary.by_market_type),
            ("by signal_type", summary.by_signal_type),
            ("by confidence bucket", summary.by_confidence_bucket),
            ("by game stage", summary.by_game_stage),
        ):
            if not cohorts:
                continue
            print(f"  {title} (unpaired):")
            for row in cohorts:
                print(
                    f"    {row.cohort:<28} base n={row.baseline.count_scored:<3} "
                    f"brier={row.baseline.mean_brier}  chal n={row.challenger.count_scored:<3} "
                    f"brier={row.challenger.mean_brier}  d_brier={row.delta_brier} "
                    f"[{row.sample_label}]"
                )
        print(f"  note: {summary.interpretation}")
        return min(
            summary.baseline.scored.count_scored, summary.challenger.scored.count_scored
        )
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
    growth_parser = subparsers.add_parser(
        "db-growth-report",
        help="OPS-011: DB size/growth, tick age+domain buckets, retention windows, alert thresholds",
    )
    growth_parser.add_argument("--top", type=int, default=12, help="largest N tables to list")
    recent_parser = subparsers.add_parser("signals-recent", help="List recent signals")
    recent_parser.add_argument("--limit", type=int, default=20)
    recent_parser.add_argument("--status", type=str, default=None)
    promote_parser = subparsers.add_parser(
        "promote-signals", help="Promote top-N new signals by priority"
    )
    promote_parser.add_argument("--limit", type=int, default=5)
    process_parser = subparsers.add_parser(
        "process-promoted-signals",
        help="Refresh enrichment/research/forecast for promoted signals",
    )
    process_parser.add_argument("--limit", type=int, default=5)
    subparsers.add_parser("signal-report", help="Aggregate signal-workflow report")
    subparsers.add_parser(
        "research-canary-report", help="External-research canary metrics by collector"
    )
    subparsers.add_parser(
        "agent-context", help="Print the project canon for coding/ops agents"
    )
    cc_parser = subparsers.add_parser(
        "champion-challenger-report",
        help="Compare a challenger forecaster against the baseline on resolved markets",
    )
    cc_parser.add_argument("--baseline", type=str, default="template_baseline")
    cc_parser.add_argument("--challenger", type=str, default="baseball_evidence_v1")
    cc_parser.add_argument("--domain", type=str, default=None)
    cc_parser.add_argument("--paired-only", action="store_true")
    cc_parser.add_argument("--min-count", type=int, default=30)
    crypto_scan_parser = subparsers.add_parser(
        "crypto-scan-once",
        help="One read-only crypto discovery pass (tokens/pairs/ticks/signals)",
    )
    crypto_scan_parser.add_argument("--limit", type=int, default=None)
    crypto_recent_parser = subparsers.add_parser(
        "crypto-signals-recent", help="List recent crypto signals"
    )
    crypto_recent_parser.add_argument("--limit", type=int, default=20)
    subparsers.add_parser(
        "crypto-report", help="Aggregate crypto surveillance report"
    )
    crypto_risk_parser = subparsers.add_parser(
        "crypto-risk-assess",
        help="Assess risk for recently-seen tokens (read-only risk intelligence)",
    )
    crypto_risk_parser.add_argument("--limit", type=int, default=50)
    subparsers.add_parser("crypto-risk-report", help="Aggregate crypto risk report")
    subparsers.add_parser(
        "crypto-provider-health-report",
        help="Crypto risk provider coverage/health (explicit gaps; read-only)",
    )
    subparsers.add_parser(
        "crypto-provider-budget-report",
        help="SolanaTracker request accounting + budget status (read-only cost/usage)",
    )
    mrc_parser = subparsers.add_parser(
        "meme-risk-coverage-report",
        help="Holder-risk coverage for the meme-news lane (read-only)",
    )
    mrc_parser.add_argument("--hours", type=int, default=24)
    subparsers.add_parser(
        "marketops-run-once",
        help="One MarketOps Autopilot cycle (read-only coordination)",
    )
    subparsers.add_parser("marketops-report", help="Aggregate MarketOps report")
    mo_alerts_parser = subparsers.add_parser(
        "marketops-alerts", help="List recent MarketOps alerts"
    )
    mo_alerts_parser.add_argument("--limit", type=int, default=20)
    mo_alerts_parser.add_argument("--status", type=str, default=None)
    mo_resolve_parser = subparsers.add_parser(
        "marketops-resolve-alert", help="Resolve one MarketOps alert by id"
    )
    mo_resolve_parser.add_argument("alert_id", type=int)
    mo_loop_parser = subparsers.add_parser(
        "marketops-loop",
        help="Autopilot loop (requires ENABLE_MARKETOPS_AUTOPILOT=true)",
    )
    mo_loop_parser.add_argument("--interval", type=int, default=None)
    edge_parser = subparsers.add_parser(
        "edge-precheck",
        help="Probability-gap measurement snapshots (measurement only, not advice)",
    )
    edge_parser.add_argument("--limit", type=int, default=50)
    edge_parser.add_argument("--force-readonly", action="store_true")
    edge_parser.add_argument("--forecast-id", type=int, default=None)
    edge_parser.add_argument(
        "--forecast-ids", type=str, default=None, help="comma-separated forecast ids"
    )
    edge_parser.add_argument("--latest-marketops-run", action="store_true")
    edge_parser.add_argument("--marketops-run-id", type=int, default=None)
    edge_parser.add_argument("--recent-refreshed-signals", action="store_true")
    subparsers.add_parser(
        "edge-precheck-report", help="Aggregate gap-measurement report"
    )
    eval_parser = subparsers.add_parser(
        "frontier-eval-report",
        help="Full-desk evaluation report (evaluation only, never advice)",
    )
    eval_parser.add_argument("--hours", type=int, default=24)
    eval_parser.add_argument("--domain", action="append", default=None)
    eval_parser.add_argument("--include-crypto", action="store_true")
    eval_parser.add_argument("--include-safety", action="store_true")
    eval_parser.add_argument("--save-run", action="store_true")
    cohort_parser = subparsers.add_parser(
        "edge-cohort-report",
        help="Edge-precheck cohort follow-through analysis (analysis only, never advice)",
    )
    cohort_parser.add_argument("--hours", type=int, default=24)
    policy_parser = subparsers.add_parser(
        "edge-policy-report",
        help="Edge shadow-policy analysis (read-only cohort-filter simulation; never advice)",
    )
    policy_parser.add_argument("--hours", type=int, default=24)
    meme_scan_parser = subparsers.add_parser(
        "meme-scan-once",
        help="One read-only meme/news attention scan (MEME-NEWS-001; never advice)",
    )
    meme_scan_parser.add_argument("--limit", type=int, default=None)
    subparsers.add_parser(
        "meme-scout-report", help="Aggregate meme attention report (read-only)"
    )
    subparsers.add_parser(
        "catalyst-report", help="Aggregate catalyst-event report (read-only)"
    )
    subparsers.add_parser(
        "domain-scout-report",
        help="Read-only market-domain inventory + candidate canary priority",
    )
    meme_news_parser = subparsers.add_parser(
        "meme-news-run-once",
        help="One bounded read-only meme/news discovery cycle (MEME-NEWS-002)",
    )
    meme_news_parser.add_argument(
        "--scheduled", action="store_true",
        help="timer mode: refuse unless ENABLE_MEME_NEWS_SCOUT=true",
    )
    meme_news_parser.add_argument("--limit", type=int, default=None)
    mn_report_parser = subparsers.add_parser(
        "meme-news-report", help="Windowed meme/news discovery report (read-only)"
    )
    mn_report_parser.add_argument("--hours", type=int, default=24)
    mn_alerts_parser = subparsers.add_parser(
        "meme-news-alerts", help="Derived notable-event report (read-only, informational)"
    )
    mn_alerts_parser.add_argument("--hours", type=int, default=6)
    mas_report_parser = subparsers.add_parser(
        "meme-mas-report",
        help="Read-only multi-agent memecoin DIAGNOSTIC review (MEME-MAS-001; not advice)",
    )
    mas_report_parser.add_argument("--hours", type=int, default=24)
    mas_report_parser.add_argument("--top", type=int, default=10)
    mas_assess_parser = subparsers.add_parser(
        "meme-mas-assess", help="Per-token MEME-MAS diagnostic traces (read-only)"
    )
    mas_assess_parser.add_argument("--limit", type=int, default=20)
    mas_assess_parser.add_argument("--hours", type=int, default=24)
    shadow_parser = subparsers.add_parser(
        "meme-shadow-report",
        help="Read-only follow-through/calibration of MEME-MAS review_priority labels (MEME-SHADOW-001; measurement, not advice)",
    )
    shadow_parser.add_argument("--lookback-hours", type=int, default=48)
    shadow_parser.add_argument("--top", type=int, default=8)
    calib_parser = subparsers.add_parser(
        "meme-mas-calibration-report",
        help="Before(v1)/after(v2) MEME-MAS review_priority calibration via MEME-SHADOW (read-only)",
    )
    calib_parser.add_argument("--lookback-hours", type=int, default=48)
    obj_parser = subparsers.add_parser(
        "meme-mas-objectives-report",
        help="Multi-objective MEME-MAS review_priority calibration (momentum/survival/risk-adjusted/queue/coverage), v1 vs v2 (read-only)",
    )
    obj_parser.add_argument("--lookback-hours", type=int, default=48)
    pm_scan_parser = subparsers.add_parser(
        "polymarket-scan-once",
        help="One bounded read-only Polymarket market-data scan (POLY-001/POLY-COVERAGE-001; never advice)",
    )
    pm_scan_parser.add_argument(
        "--scheduled", action="store_true",
        help="timer mode: refuse unless ENABLE_POLYMARKET_SCOUT=true",
    )
    pm_scan_parser.add_argument(
        "--limit", type=int, default=None,
        help="max markets persisted this scan (bounded; targeted queries claim budget first)",
    )
    pm_scan_parser.add_argument(
        "--orderbook-limit", type=int, default=None,
        help="max CLOB order books read this scan (hard cap; reads books, never places orders)",
    )
    pm_scan_parser.add_argument(
        "--category", type=int, default=None, metavar="TAG_ID",
        help="scope the catalog walk to a Polymarket category (Gamma tag_id)",
    )
    pm_scan_parser.add_argument(
        "--active-only", dest="active_only", action="store_true", default=True,
        help="only active markets (default)",
    )
    pm_scan_parser.add_argument(
        "--no-active-only", dest="active_only", action="store_false",
        help="do not filter to active markets",
    )
    pm_scan_parser.add_argument(
        "--include-closed", dest="include_closed", action="store_true", default=False,
        help="also observe closed/resolved markets (settled reference data; still read-only)",
    )
    pm_scan_parser.add_argument(
        "--query", action="append", default=None, metavar="TEXT",
        help="read-only public-search query (repeatable)",
    )
    pm_scan_parser.add_argument(
        "--targeted", action="store_true",
        help="derive search queries deterministically from persisted Kalshi active titles (no LLM)",
    )
    pm_scan_parser.add_argument(
        "--end-date-min", type=str, default=None, metavar="ISO8601",
        help="only markets resolving at/after this time (resolution-window coverage filter)",
    )
    pm_scan_parser.add_argument(
        "--end-date-max", type=str, default=None, metavar="ISO8601",
        help="only markets resolving at/before this time",
    )
    pm_report_parser = subparsers.add_parser(
        "polymarket-report", help="Windowed Polymarket market-data report (read-only)"
    )
    pm_report_parser.add_argument("--hours", type=int, default=24)
    subparsers.add_parser(
        "polymarket-domain-report",
        help="Read-only Polymarket per-domain/category inventory (latest scan)",
    )
    pm_coverage_parser = subparsers.add_parser(
        "polymarket-coverage-report",
        help="Read-only Polymarket/Kalshi coverage census (POLY-COVERAGE-001; supply counts, never advice)",
    )
    pm_coverage_parser.add_argument("--top", type=int, default=30)
    pm_coverage_parser.add_argument(
        "--kalshi-limit", type=int, default=4000,
        help="max Kalshi markets in the census (truncation is reported, never silent)",
    )
    cv_match_parser = subparsers.add_parser(
        "cross-venue-match-once",
        help="One read-only Kalshi<->Polymarket cross-venue matching/observation pass (POLY-002; not advice/arb/EV)",
    )
    cv_match_parser.add_argument("--kalshi-limit", type=int, default=4000)
    cv_match_parser.add_argument("--polymarket-limit", type=int, default=500)
    cv_match_parser.add_argument(
        "--recent-hours", type=int, default=None,
        help="only consider Kalshi markets seen within this many hours (drops stale 'active' rows)",
    )
    cv_match_parser.add_argument(
        "--domain", type=str, default=None,
        help="narrow the sample to one coarse domain (sports/politics/crypto/economics/other)",
    )
    cv_match_parser.add_argument(
        "--market-type", type=str, default=None,
        help="narrow the sample to one outcome type (e.g. winner/over_under/yes_no/exact_score)",
    )
    subparsers.add_parser(
        "cross-venue-report", help="Read-only cross-venue observation report (POLY-002)"
    )
    xv_obs_parser = subparsers.add_parser(
        "xvenue-observation-report",
        help="Read-only observation-window report: did the latest scan+match produce clean comparables? (XVENUE-OBS-001; never advice/arb/EV)",
    )
    xv_obs_parser.add_argument("--top", type=int, default=10)
    cv_cand_parser = subparsers.add_parser(
        "cross-venue-candidates", help="List cross-venue candidates from the latest run (read-only)"
    )
    cv_cand_parser.add_argument("--label", type=str, default=None)
    subparsers.add_parser(
        "backup-db", help="Compressed timestamped SQLite backup (+retention pruning)"
    )
    subparsers.add_parser("list-db-backups", help="List existing DB backups")
    verify_backup_parser = subparsers.add_parser(
        "verify-db-backup", help="Verify a backup is readable and complete"
    )
    verify_backup_parser.add_argument("path", type=str)
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
    if args.command == "db-growth-report":
        total = asyncio.run(db_growth_report(top=args.top))
        return 0 if total >= 0 else 1
    if args.command == "signals-recent":
        count = asyncio.run(signals_recent(limit=args.limit, signal_status=args.status))
        return 0 if count >= 0 else 1
    if args.command == "promote-signals":
        count = asyncio.run(promote_signals(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "process-promoted-signals":
        count = asyncio.run(process_promoted_signals(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "signal-report":
        total = asyncio.run(signal_report())
        return 0 if total >= 0 else 1
    if args.command == "research-canary-report":
        total = asyncio.run(research_canary_report())
        return 0 if total >= 0 else 1
    if args.command == "agent-context":
        return asyncio.run(agent_context())
    if args.command == "champion-challenger-report":
        count = asyncio.run(
            champion_challenger_report(
                baseline=args.baseline,
                challenger=args.challenger,
                domain=args.domain,
                paired_only=args.paired_only,
                min_count=args.min_count,
            )
        )
        return 0 if count >= 0 else 1
    if args.command == "crypto-scan-once":
        return asyncio.run(crypto_scan_once(limit=args.limit))
    if args.command == "crypto-signals-recent":
        count = asyncio.run(crypto_signals_recent(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "crypto-report":
        total = asyncio.run(crypto_report())
        return 0 if total >= 0 else 1
    if args.command == "crypto-risk-assess":
        count = asyncio.run(crypto_risk_assess(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "crypto-provider-health-report":
        return asyncio.run(crypto_provider_health_report())
    if args.command == "crypto-provider-budget-report":
        return asyncio.run(crypto_provider_budget_report())
    if args.command == "meme-risk-coverage-report":
        n = asyncio.run(meme_risk_coverage_report(hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "crypto-risk-report":
        total = asyncio.run(crypto_risk_report())
        return 0 if total >= 0 else 1
    if args.command == "marketops-run-once":
        return asyncio.run(marketops_run_once())
    if args.command == "marketops-report":
        total = asyncio.run(marketops_report())
        return 0 if total >= 0 else 1
    if args.command == "marketops-alerts":
        count = asyncio.run(marketops_alerts(limit=args.limit, alert_status=args.status))
        return 0 if count >= 0 else 1
    if args.command == "marketops-resolve-alert":
        return asyncio.run(marketops_resolve_alert(alert_id=args.alert_id))
    if args.command == "marketops-loop":
        iterations = asyncio.run(marketops_loop(interval=args.interval))
        return 0 if iterations >= 0 else 1
    if args.command == "edge-precheck":
        ids: list[int] | None = None
        if args.forecast_id is not None:
            ids = [args.forecast_id]
        elif args.forecast_ids:
            ids = [int(part) for part in args.forecast_ids.split(",") if part.strip()]
        count = asyncio.run(
            edge_precheck(
                limit=args.limit,
                force_readonly=args.force_readonly,
                forecast_ids=ids,
                marketops_run_id=args.marketops_run_id,
                latest_marketops_run=args.latest_marketops_run,
                recent_refreshed_signals=args.recent_refreshed_signals,
            )
        )
        return 0 if count >= 0 else 1
    if args.command == "edge-precheck-report":
        total = asyncio.run(edge_precheck_report())
        return 0 if total >= 0 else 1
    if args.command == "frontier-eval-report":
        return asyncio.run(
            frontier_eval_report(
                hours=args.hours,
                domains=args.domain,
                include_crypto=args.include_crypto,
                include_safety=args.include_safety,
                save_run=args.save_run,
            )
        )
    if args.command == "edge-cohort-report":
        return asyncio.run(edge_cohort_report(hours=args.hours))
    if args.command == "edge-policy-report":
        return asyncio.run(edge_policy_report(hours=args.hours))
    if args.command == "meme-scan-once":
        scored = asyncio.run(meme_scan_once(limit=args.limit))
        return 0 if scored >= 0 else 1
    if args.command == "meme-scout-report":
        total = asyncio.run(meme_scout_report())
        return 0 if total >= 0 else 1
    if args.command == "catalyst-report":
        total = asyncio.run(catalyst_report())
        return 0 if total >= 0 else 1
    if args.command == "domain-scout-report":
        n = asyncio.run(domain_scout_report())
        return 0 if n >= 0 else 1
    if args.command == "meme-news-run-once":
        scored = asyncio.run(meme_news_run_once(scheduled=args.scheduled, limit=args.limit))
        return 0 if scored >= 0 else 1
    if args.command == "meme-news-report":
        n = asyncio.run(meme_news_report(hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "meme-news-alerts":
        n = asyncio.run(meme_news_alerts(hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "meme-mas-report":
        n = asyncio.run(meme_mas_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "meme-mas-assess":
        n = asyncio.run(meme_mas_assess(limit=args.limit, hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "meme-shadow-report":
        n = asyncio.run(meme_shadow_report(lookback_hours=args.lookback_hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "meme-mas-calibration-report":
        n = asyncio.run(meme_mas_calibration_report(lookback_hours=args.lookback_hours))
        return 0 if n >= 0 else 1
    if args.command == "meme-mas-objectives-report":
        n = asyncio.run(meme_mas_objectives_report(lookback_hours=args.lookback_hours))
        return 0 if n >= 0 else 1
    if args.command == "polymarket-scan-once":
        scored = asyncio.run(
            polymarket_scan_once(
                scheduled=args.scheduled,
                limit=args.limit,
                orderbook_limit=args.orderbook_limit,
                category=args.category,
                active_only=args.active_only,
                include_closed=args.include_closed,
                query=args.query,
                targeted=args.targeted,
                end_date_min=args.end_date_min,
                end_date_max=args.end_date_max,
            )
        )
        return 0 if scored >= 0 else 1
    if args.command == "polymarket-report":
        n = asyncio.run(polymarket_report(hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "polymarket-domain-report":
        n = asyncio.run(polymarket_domain_report())
        return 0 if n >= 0 else 1
    if args.command == "polymarket-coverage-report":
        n = asyncio.run(polymarket_coverage_report(top=args.top, kalshi_limit=args.kalshi_limit))
        return 0 if n >= 0 else 1
    if args.command == "cross-venue-match-once":
        n = asyncio.run(cross_venue_match_once(
            kalshi_limit=args.kalshi_limit, polymarket_limit=args.polymarket_limit,
            recent_hours=args.recent_hours, domain=args.domain, market_type=args.market_type,
        ))
        return 0 if n >= 0 else 1
    if args.command == "cross-venue-report":
        n = asyncio.run(cross_venue_report())
        return 0 if n >= 0 else 1
    if args.command == "xvenue-observation-report":
        n = asyncio.run(xvenue_observation_report(top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "cross-venue-candidates":
        n = asyncio.run(cross_venue_candidates(label=args.label))
        return 0 if n >= 0 else 1
    if args.command == "backup-db":
        return asyncio.run(backup_db())
    if args.command == "list-db-backups":
        count = asyncio.run(list_db_backups())
        return 0 if count >= 0 else 1
    if args.command == "verify-db-backup":
        return asyncio.run(verify_db_backup(path=args.path))
    return 2


if __name__ == "__main__":
    sys.exit(main())
