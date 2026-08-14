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
from pathlib import Path as _Path
import logging
import sys

from app.adapters.kalshi import KalshiRestAdapter
from app.models import ScannerRun

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

TOP_N_PRINTED = 20


# NEW-HIGH-2 fix (third Lane-B review, SQLite coexistence). `batch_size` IS
# the write-lock safety argument the crypto tape reconciler rests on
# (`run_scheduled_reconciliation` now validates it at runtime too — see
# app/services/crypto_tape.py), and `--max-duration-seconds` gates how long
# a single write-lock-holding pass can run. Fail fast at ARGPARSE time with
# a clear CLI error, rather than only at the deeper `_refused()` call —
# an operator typo (`--batch-size 0`, `--batch-size -5`) should never even
# reach a DB session.
def _positive_int_arg(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def _non_negative_float_arg(raw: str) -> float:
    value = float(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


# CRYPTO-COVERAGE-REPAIR-001 B1/B3/B11: the adaptive time-budgeted batching
# knobs (`--time-budget-seconds`/`--initial-per-token-cost-seconds`) are
# strictly positive by definition — unlike `--max-duration-seconds`, 0.0 has
# no legitimate meaning for either (a zero write-time budget or a zero
# per-token cost estimate is nonsensical, not an intentional "already past
# due" sentinel). Fail at argparse time, same as `_positive_int_arg`.
def _positive_float_arg(raw: str) -> float:
    value = float(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


def _registry_print_validation(v, warnings_only=False):
    if v.errors:
        print(f"REJECTED ({len(v.errors)} error(s)):")
        for e in v.errors:
            print(f"  - {e}")
    for w in v.warnings:
        print(f"  flag: {w}")


def experiment_registry_validate(manifest: str, fmt: str = "text") -> int:
    """PROSPECTIVE-EXPERIMENT-REGISTRY-001 — validate a manifest. Pure."""
    import json as _json

    from app.services.experiment_registry import (
        ManifestError,
        load_manifest,
        validate_manifest,
    )

    try:
        data = load_manifest(_Path(manifest))
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}")
        return 2
    v = validate_manifest(data)
    if fmt == "json":
        print(_json.dumps(v.to_dict(), indent=2, sort_keys=True))
        return 0 if v.ok else 1
    print(f"manifest: {manifest}")
    print(f"experiment_id: {v.experiment_id}")
    if v.ok:
        print(f"VALID — digest {v.digest}")
    _registry_print_validation(v)
    return 0 if v.ok else 1


def experiment_registry_register(
    manifest: str, confirm: bool = False, base: str | None = None,
    fmt: str = "text",
) -> int:
    """Register a manifest. Without --confirm this writes nothing."""
    import json as _json
    import subprocess

    from app.services.experiment_registry import (
        ManifestError,
        load_manifest,
        register,
    )

    try:
        data = load_manifest(_Path(manifest))
    except (ManifestError, OSError) as exc:
        print(f"error: {exc}")
        return 2
    commit = None
    if confirm:
        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                check=True, timeout=10).stdout.strip()
        except Exception:
            commit = None
    try:
        out = register(data, base=_Path(base) if base else None,
                       confirm=confirm, commit=commit)
    except ManifestError as exc:
        print(f"REFUSED: {exc}")
        return 1
    if fmt == "json":
        print(_json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0
    print(f"experiment {out['experiment_id']} — {out['mode'].upper()}")
    print(out["disclaimer"])
    print(f"  manifest_digest   {out['manifest_digest']}")
    print(f"  state             {out['state']}")
    print(f"  registered_at     {out['registered_at']}")
    print(f"  registration_commit {out['registration_commit']}")
    print(f"  persisted         {str(out['persisted']).lower()}")
    for w in out.get("warnings", []):
        print(f"  flag: {w}")
    if not confirm:
        print("  nothing written — re-run with --confirm to register")
        print("  review the digest above; it is what the registration binds")
    return 0


def kalshi_realtime_replay(
    archive: str, environment: str = "demo", fmt: str = "text",
) -> int:
    """KALSHI-REALTIME-OBSERVATION-001A — deterministic replay of an archive.

    Pure: no network, no credential, no database, no venue. The same archive
    must always produce the same per-market checksums — that equality is the
    acceptance test for the whole data path, not a debugging convenience.
    """
    import json as _json

    from app.realtime.archive import (
        ArchiveError,
        EventArchive,
        latency_envelope,
        replay,
    )

    try:
        store = EventArchive(_Path(archive), environment=environment)
        records = store.read_all()
    except (ArchiveError, OSError) as exc:
        print(f"error: {exc}")
        return 2

    integrity = store.verify()
    out = replay(records)
    lat = latency_envelope(records).to_dict()
    payload = {"archive": str(archive), "environment": environment,
               "integrity": integrity, "replay": out, "latency": lat,
               "external_calls": 0, "persisted": False}

    if fmt == "json":
        print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if integrity["intact"] and not out["faults"] else 1

    print(f"kalshi realtime replay — {environment}")
    print("  read-only reconstruction; zero provider calls, zero writes")
    print(f"  archive              {archive}")
    print(f"  records              {integrity['records']}")
    print(f"  digest mismatches    {len(integrity['mismatched'])}")
    print(f"  truncated records    {integrity['truncated_records']}")
    print(f"  markets              {out['markets']}")
    print(f"  events applied       {out['events_applied']}")
    print(f"  events rejected      {out['events_rejected']}")
    for ticker, chk in out["checksums"].items():
        pub = out["publishable"][ticker]
        st = out["stats"][ticker]
        print(f"    {ticker:24} publishable={pub} checksum={chk[:16]}…")
        print(f"      snapshots={st['snapshots']} deltas={st['deltas']} "
              f"dups={st['duplicates']} gaps={st['gaps']} "
              f"regressions={st['regressions']}")
    if out["faults"]:
        print("  faults (never absorbed silently):")
        for f in out["faults"][:10]:
            print(f"    {f['market_ticker']} seq={f['seq']}: {f['error']}")
    print("  latency envelope (decomposed, never one number):")
    for hop in ("venue_to_receive_offset_contaminated_ms",
                "receive_to_normalize_us"):
        q = lat[hop]
        print(f"    {hop:42} n={q['n']} p50={q['p50']} p95={q['p95']} "
              f"p99={q['p99']} max={q['max']} mean={q['mean']} "
              f"negative={q['negative']}")
    cov = lat["coverage"]
    print(f"    coverage: venue_time on {cov['records_with_venue_time']} of "
          f"{lat['records_scanned']} records")
    if not cov["observation_gaps_measured"]:
        print("    NOT MEASURED: reconnect/observation gaps. Every percentile "
              "above is conditioned on being connected.")
    if not cov["host_clock_offset_characterised"]:
        print("    NOT MEASURED: host clock offset. The venue hop is offset "
              "contaminated and is evidence, not a latency.")
    return 0 if integrity["intact"] and not out["faults"] else 1


def archive_init(
    root: str, environment: str = "demo",
    archive_identity: str = "kalshi-realtime",
    confirm: bool = False, fmt: str = "text",
) -> int:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7 — explicitly initialize an
    archive root. The ONLY way a genesis is minted.

    An uninitialized root is a HALT, never an invitation to bootstrap
    silently: the collector consumes an initialized archive and can never
    create one on its own. This command is the one place that gap is closed,
    and only under `--confirm`. Re-running against an already-initialized
    root refuses (fails closed) rather than minting a second root of trust
    or silently no-opping over real history.
    """
    import json as _json

    from app.realtime.archive_head import ArchiveHeadError, head_state

    root_path = _Path(root)
    state = head_state(root_path, environment=environment)
    if not confirm:
        payload = {"mode": "dry_run", "root": str(root_path),
                  "environment": environment,
                  "archive_identity": archive_identity,
                  "current_state": state.get("state")}
        if fmt == "json":
            print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"DRY RUN archive-init {root_path} [{environment}]")
            print(f"  current head_state: {state.get('state')}")
            if state.get("state") == "NOT_INITIALIZED":
                print(f"  would mint a new genesis (archive_identity="
                      f"{archive_identity!r})")
            else:
                print("  refuses: this root is already initialized")
            print("  nothing written — re-run with --confirm to apply")
        return 0
    try:
        genesis = initialize_archive_(root_path, environment,
                                      archive_identity=archive_identity)
    except ArchiveHeadError as exc:
        print(f"refused: {exc}")
        return 1
    payload = {"mode": "confirmed", "root": str(root_path),
              "environment": environment, "archive_id": genesis["archive_id"],
              "archive_identity": genesis["archive_identity"],
              "created_at": genesis["created_at"]}
    if fmt == "json":
        print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"CONFIRMED archive-init {root_path} [{environment}]")
    print(f"  archive_id     {genesis['archive_id']}")
    print(f"  archive_identity {genesis['archive_identity']}")
    print(f"  created_at     {genesis['created_at']}")
    return 0


def initialize_archive_(root, environment, *, archive_identity):
    """Thin import seam so tests can monkeypatch this without touching the
    heavier `app.realtime.archive_head` module import graph."""
    from app.realtime.archive_head import initialize_archive

    return initialize_archive(root, environment, archive_identity=archive_identity)


def archive_recover_head(
    root: str, environment: str = "demo",
    confirm: bool = False, fmt: str = "text",
) -> int:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7 — finish an interrupted head
    transition. Deterministic, and safe to run when nothing is wrong.

    Wraps `recover_current_head` exactly, adding nothing: it advances the
    current-head pointer to a generation N only when N's record is already
    durably on disk, chains from the current generation, and its committed
    segment verifies. It never reads segment directories to invent a
    history. If no transition is outstanding it is a no-op — this command
    is idempotent by construction, not by a special case here.
    """
    import json as _json

    from app.realtime.archive_head import ArchiveHeadError, head_state

    root_path = _Path(root)
    before = head_state(root_path, environment=environment)
    if not confirm:
        payload = {"mode": "dry_run", "root": str(root_path),
                  "environment": environment,
                  "head_state": before.get("state"),
                  "reason": before.get("reason")}
        if fmt == "json":
            print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"DRY RUN archive-recover-head {root_path} [{environment}]")
            print(f"  head_state: {before.get('state')}")
            if before.get("state") == "STALE_HEAD":
                print("  would advance the pointer to the durable generation "
                      "already on disk")
            elif before.get("state") == "RECOVERY_REQUIRED":
                print("  would re-point at the newest immutable generation "
                      "record (the current-head pointer is missing)")
            else:
                print("  no recovery action applies to this state")
            print("  nothing written — re-run with --confirm to apply")
        return 0
    try:
        record = recover_current_head_(root_path, environment)
    except ArchiveHeadError as exc:
        print(f"refused: {exc}")
        return 1
    after = head_state(root_path, environment=environment)
    payload = {"mode": "confirmed", "root": str(root_path),
              "environment": environment,
              "generation_before": before.get("head", {}).generation
              if before.get("head") else None,
              "generation_after": record["generation"],
              "head_state_after": after.get("state")}
    if fmt == "json":
        print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0 if after.get("state") == "CURRENT" else 1
    print(f"CONFIRMED archive-recover-head {root_path} [{environment}]")
    print(f"  generation now  {record['generation']}")
    print(f"  head_state      {after.get('state')}")
    return 0 if after.get("state") == "CURRENT" else 1


def recover_current_head_(root, environment):
    """Import seam, matching `initialize_archive_` above."""
    from app.realtime.archive_head import recover_current_head

    return recover_current_head(root, environment)


def archive_adopt(
    root: str, segment_id: str, environment: str = "demo",
    confirm: bool = False, fmt: str = "text",
) -> int:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7 — adopt ONE orphaned committed
    segment into the archive's authoritative history.

    An ORPHANED_COMMITTED_SEGMENT is a segment whose manifest is durable
    evidence on disk that no generation record commits (a crash between the
    manifest publish and the head commit, or a graft). Adopting it commits
    that segment's own already-published, already-verified manifest through
    the ordinary `commit_segment` path -- the same path every live write
    uses; nothing here grants the segment a guarantee an ordinary commit
    would not, and nothing here reads its bytes differently.

    Bounded to exactly the state it exists for: this refuses unless
    `verify_archive` currently reports `segment_id` in
    `orphaned_committed_segments`. It cannot adopt an arbitrary segment id,
    and re-running it after a successful adopt finds the segment no longer
    orphaned and refuses cleanly (idempotent: a second commit is never
    attempted).

    Never discards evidence. There is a deliberate asymmetry here: adopting
    is committing evidence that already exists, which this codebase already
    knows how to verify safely. Discarding is deleting evidence, which has
    no reviewed primitive anywhere in this codebase, and is not one this
    command invents -- a graft, as opposed to crash residue, is resolved by
    an operator with direct, reviewed filesystem access, not a scripted
    one-line delete.
    """
    import json as _json

    from app.realtime.archive_head import ArchiveHeadError
    from app.realtime.canonical import parse_canonical
    from app.realtime.segment import MANIFEST_FILENAME, verify_archive, verify_segment
    from app.realtime import evidence_fs

    root_path = _Path(root)
    report = verify_archive(root_path, environment=environment)
    orphaned = report.get("orphaned_committed_segments", [])
    if segment_id not in orphaned:
        payload = {"mode": "refused", "root": str(root_path),
                  "environment": environment, "segment_id": segment_id,
                  "orphaned_committed_segments": orphaned,
                  "reason": (
                      f"segment {segment_id!r} is not currently reported as an "
                      "orphaned committed segment; refusing to adopt a segment "
                      "this operation was not asked to repair")}
        if fmt == "json":
            print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"refused: {payload['reason']}")
            print(f"  currently orphaned: {orphaned}")
        return 1

    seg_dir = root_path / f"env={environment}" / f"segment={segment_id}"
    manifest_bytes, why = evidence_fs.bounded_read(seg_dir / MANIFEST_FILENAME)
    if why is not None:
        print(f"refused: manifest for {segment_id!r} is unreadable: {why}")
        return 1
    manifest = parse_canonical(manifest_bytes)

    verdict = verify_segment(seg_dir, environment=environment, root=root_path)
    if not verdict.valid:
        print(f"refused: segment {segment_id!r} does not verify independently "
              f"of the archive head; refusing to adopt it: {verdict.reasons}")
        return 1

    if not confirm:
        payload = {"mode": "dry_run", "root": str(root_path),
                  "environment": environment, "segment_id": segment_id,
                  "record_count": manifest.get("record_count"),
                  "partition_identity": manifest.get("partition_identity"),
                  "manifest_digest": manifest.get("manifest_digest")}
        if fmt == "json":
            print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(f"DRY RUN archive-adopt {segment_id!r} [{environment}]")
            print(f"  record_count       {manifest.get('record_count')}")
            print(f"  partition_identity {manifest.get('partition_identity')}")
            print(f"  manifest_digest    {manifest.get('manifest_digest')}")
            print("  nothing written — re-run with --confirm to commit this "
                  "segment into history")
        return 0

    try:
        head = commit_segment_(root_path, environment, manifest=manifest)
    except ArchiveHeadError as exc:
        print(f"refused: {exc}")
        return 1
    payload = {"mode": "confirmed", "root": str(root_path),
              "environment": environment, "segment_id": segment_id,
              "generation": head["generation"]}
    if fmt == "json":
        print(_json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"CONFIRMED archive-adopt {segment_id!r} [{environment}]")
    print(f"  generation now {head['generation']}")
    return 0


def commit_segment_(root, environment, *, manifest):
    """Import seam, matching `initialize_archive_`/`recover_current_head_`."""
    from app.realtime.archive_head import commit_segment

    return commit_segment(root, environment, manifest=manifest)


def archive_migrate_legacy(
    source: str, dest: str, environment: str = "demo",
    archive_identity: str = "kalshi-realtime",
    confirm: bool = False, fmt: str = "text",
) -> int:
    """KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7 — explicit offline import of a
    pre-genesis legacy archive into a NEW governed archive.

    Thin wrapper over `migrate_legacy_archive`: the legacy source is opened
    read-only and never written, moved or deleted; the destination is always
    a brand-new archive with its own genesis (never an extension of an
    existing one); and the provenance record states exactly what the
    pre-genesis format could not prove, rather than implying the imported
    evidence carries guarantees it never had.
    """
    import json as _json

    from app.realtime.legacy_import import LegacyImportError, migrate_legacy_archive

    try:
        result = migrate_legacy_archive(
            source, dest, environment=environment,
            archive_identity=archive_identity, confirm=confirm)
    except LegacyImportError as exc:
        print(f"refused: {exc}")
        return 1
    if fmt == "json":
        print(_json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    print(f"{'CONFIRMED' if confirm else 'DRY RUN'} archive-migrate-legacy "
          f"{source} -> {dest} [{environment}]")
    print(f"  files                 {len(result.get('files', []))}")
    print(f"  records_readable      {result.get('records_readable')}")
    print(f"  records_rejected      {result.get('records_rejected')}")
    print(f"  torn_files            {result.get('torn_files')}")
    print(f"  undecodable_files     {result.get('undecodable_files')}")
    print(f"  first_timestamp       {result.get('first_timestamp')}")
    print(f"  last_timestamp        {result.get('last_timestamp')}")
    if confirm:
        print(f"  archive_id            {result.get('archive_id')}")
        print(f"  records_imported      {result.get('records_imported')}")
        print(f"  records_refused       {result.get('records_refused_at_import')}")
        print(f"  migration_timestamp   {result.get('migration_timestamp')}")
    else:
        print(f"  would_import          {result.get('would_import')}")
        print("  nothing written — re-run with --confirm to create the new "
              "archive and import")
    for limitation in result.get("source_limitations", []):
        print(f"  LIMITATION: {limitation}")
    return 0


def experiment_registry_record_result(
    experiment_id: str, confirm: bool = False, base: str | None = None,
    notes: str | None = None, reevaluation_reason: str | None = None,
    fmt: str = "text",
) -> int:
    """PROSPECTIVE-EXPERIMENT-REGISTRY-002B — registry-owned evaluation.

    The caller supplies an id and optional prose. It cannot supply a sample
    count, a metric value, a population, an end time or a verdict: each of those
    is a place where a null result becomes a positive one, and accepting them
    from the person who wants the answer would make this a filing cabinet rather
    than a control. Everything authoritative comes from the registered manifest.

    Zero provider calls. Reads production data; writes only registry artifacts,
    and only under --confirm.
    """
    import json as _json
    import subprocess

    from app.services.experiment_registry import ManifestError
    from app.services.experiment_results import evaluate_experiment

    owns = False
    session = None
    try:
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
        owns = True
        commit = None
        try:
            commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                    capture_output=True, text=True,
                                    timeout=10).stdout.strip()
        except Exception:
            commit = None
        out = evaluate_experiment(
            session, experiment_id, base=_Path(base) if base else None,
            confirm=confirm, operator_notes=notes,
            reevaluation_reason=reevaluation_reason, commit=commit)
    except ManifestError as exc:
        print(f"REFUSED: {exc}")
        return 1
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 2
    finally:
        if owns and session is not None:
            session.close()

    r = out["record"]
    if fmt == "json":
        print(_json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0
    print(f"experiment {experiment_id} — {out['mode'].upper()}")
    print(out["disclaimer"])
    print(f"  external_calls={out['external_calls']} "
          f"persisted={str(out['persisted']).lower()}")
    print()
    print(f"  VERDICT                  {r['verdict']}")
    for why in r.get("verdict_reasons", []):
        print(f"    because                {why}")
    print()
    print("  population (membership frozen BEFORE outcomes were read):")
    for k in ("registered_population_count", "actual_population_count",
              "pre_registration_excluded", "post_end_excluded",
              "matured_count", "pending_count", "unscorable_count",
              "matured_fraction"):
        print(f"    {k:30} {r[k]}")
    print(f"    membership_digest              {r['membership_digest']}")
    print()
    print("  metric:")
    for k in ("primary_metric_name", "primary_metric_value",
              "declared_baseline_name", "declared_baseline_value",
              "metric_delta", "decision_rule", "sample_floor",
              "sample_floor_met", "stopping_rule_met"):
        print(f"    {k:30} {r[k]}")
    ci = r.get("confidence_interval") or {}
    print(f"    confidence_interval            [{ci.get('lower')}, "
          f"{ci.get('upper')}] {ci.get('policy')}")
    print()
    print(f"  drift: {[(k, v['classification']) for k, v in r['drift_classification'].items()]}")
    if r["protocol_deviations"]:
        print("  protocol deviations:")
        for dv in r["protocol_deviations"]:
            print(f"    - {dv}")
    if r["invalidating_events"]:
        print("  invalidating events:")
        for iv in r["invalidating_events"]:
            print(f"    - {iv}")
    print(f"  result_digest {r['result_digest']}")
    if not confirm:
        print("  nothing written — re-run with --confirm to append this result")
    else:
        print(f"  appended {out.get('file')}")
    return 0


def experiment_registry_report(
    experiment_id: str, base: str | None = None, fmt: str = "text"
) -> int:
    """PROSPECTIVE-EXPERIMENT-REGISTRY-002A — full read-only experiment report.

    Specified in the REGISTRY-001 CLI contract and never implemented; that gap
    was disclosed rather than dropped and is closed here. Zero provider calls,
    zero writes, no registration, no transition. **Fails closed** (exit 1) when
    the manifest digest or the event chain is broken, because a report that
    renders cleanly over a tampered experiment is worse than no report.
    """
    import json as _json

    from app.services.experiment_population import classify_population_drift
    from app.services.experiment_predicates import (
        PREDICATE_SCHEMA_VERSION,
        PredicateError,
        canonicalize_population,
        ensure_registration_floor,
        population_digest,
    )
    from app.services.experiment_registry import (
        DISCLAIMER,
        ManifestError,
        experiment_dir,
        load_manifest,
        read_events,
        status,
        validate_manifest,
    )

    try:
        root = _Path(base) if base else None
        st = status(experiment_id, root)
        manifest = load_manifest(
            experiment_dir(experiment_id, root) / "manifest.json")
        events = read_events(experiment_id, root)
    except Exception as exc:
        print(f"error: {exc}")
        return 2

    # M5 — re-validate the AUTHORED document. The stored manifest necessarily
    # contains registry-assigned fields (start_time among them), so validating
    # it verbatim produced a permanent false error on every registered
    # experiment, which makes a real error indistinguishable from noise.
    from app.services.experiment_registry import REGISTRY_ASSIGNED_FIELDS

    authored = {k: val for k, val in manifest.items()
                if k not in REGISTRY_ASSIGNED_FIELDS}
    v = validate_manifest(authored)
    refs = manifest.get("immutable_references") or {}
    try:
        canon = ensure_registration_floor(
            canonicalize_population(manifest.get("population")))
        canon_digest = population_digest(canon)
        canon_err = None
    except PredicateError as exc:
        canon, canon_digest, canon_err = None, None, str(exc)

    drift = classify_population_drift(refs.get("population_references"))
    integrity = st["integrity"]

    # M6 — compare the live predicate digest against the one pinned at
    # registration. Cheap defence in depth against a coordinated re-forge.
    pinned_predicate = refs.get("population_predicate_digest")
    predicate_matches = (pinned_predicate is None
                         or pinned_predicate == canon_digest)

    # M3 — fail closed on the cases that actually matter to a future result
    # layer, not only on a broken digest. A manifest that is digest-intact but
    # whose population no longer canonicalizes IS the field-registry-drift
    # scenario, and it previously exited 0.
    ok = (bool(integrity["intact"]) and canon_err is None
          and not drift["material"] and predicate_matches)

    data = {
        "experiment_id": experiment_id,
        "title": manifest.get("title"),
        "manifest_version": manifest.get("experiment_version"),
        "manifest_digest": integrity["digest_recomputed_now"],
        "digest_at_registration": integrity["digest_at_registration"],
        "manifest_intact": integrity["intact"],
        "event_chain": integrity.get("event_chain"),
        "event_count": len(events),
        "registry_state": st["state"],
        "allowed_transitions": st["allowed_transitions"],
        "registered_at": st["registered_at"],
        "registration_commit": st["registration_commit"],
        "immutable_references": refs,
        "predicate_schema_version": PREDICATE_SCHEMA_VERSION,
        "canonical_population": canon,
        "population_predicate_digest": canon_digest,
        "population_error": canon_err,
        "population_drift": drift,
        "population_predicate_digest_at_registration": pinned_predicate,
        "population_predicate_digest_matches": predicate_matches,
        "fail_closed_ok": ok,
        "evaluation_code_drift": st.get("evaluation_code_drift"),
        "collection_state": (
            "active" if st["state"] == "collecting" else
            "not_started" if st["state"] in (None, "registered") else st["state"]),
        "result_state": (
            "results_not_implemented_until_registry_002b"
            if not st["results"] else "recorded"),
        "results": st["results"],
        "validation_errors": v.errors,
        "warnings": v.warnings,
        "external_calls": 0,
        "persisted": False,
        "disclaimer": DISCLAIMER,
    }

    if fmt == "json":
        print(_json.dumps(data, indent=2, sort_keys=True, default=str))
        return 0 if ok else 1

    print(f"experiment {experiment_id} — {data['title']}")
    print(data["disclaimer"])
    print(f"  external_calls={data['external_calls']} "
          f"persisted={str(data['persisted']).lower()}")
    print()
    for k in ("manifest_version", "registry_state", "registered_at",
              "registration_commit", "event_count", "collection_state",
              "result_state"):
        print(f"  {k:28} {data[k]}")
    print(f"  {'manifest_digest':28} {data['manifest_digest']}")
    print(f"  {'manifest_intact':28} {data['manifest_intact']}")
    ch = data["event_chain"] or {}
    print(f"  {'event_chain_intact':28} {ch.get('intact')} "
          f"(len={ch.get('length')}, broken_at={ch.get('broken_at')})")
    print(f"  {'predicate_schema_version':28} {data['predicate_schema_version']}")
    print(f"  {'population_digest':28} {data['population_predicate_digest']}")
    print(f"  {'population_drift':28} {drift['classification']} "
          f"(material={drift['material']})")
    print(f"  {'predicate_digest_matches':28} {predicate_matches}")
    ecd = data["evaluation_code_drift"] or {}
    print(f"  {'evaluation_code_drift':28} {ecd.get('drifted')}")
    print(f"  {'allowed_transitions':28} {data['allowed_transitions']}")
    print()
    print("  canonical population predicates:")
    if canon_err:
        print(f"    ERROR: {canon_err}")
    else:
        for clause in ("all", "none"):
            for pr in canon.get(clause, []):
                val = f" {pr['value']!r}" if "value" in pr else ""
                print(f"    {clause:5} {pr['field']} {pr['operator']}{val}")
    print()
    print("  immutable references:")
    for k in sorted(refs):
        val = refs[k]
        print(f"    {k:34} {str(val)[:70]}")
    if v.errors:
        print()
        print("  validation errors:")
        for e in v.errors:
            print(f"    - {e}")
    if v.warnings:
        print()
        print("  warnings:")
        for w in v.warnings:
            print(f"    flag: {w}")
    if not ok:
        print()
        print("  FAILED CLOSED — one of: manifest digest, event chain, "
              "population canonicalization, pinned predicate digest, or "
              "material drift. This experiment must not be evaluated.")
    return 0 if ok else 1


def experiment_registry_status(
    experiment_id: str, base: str | None = None, fmt: str = "text"
) -> int:
    import json as _json

    from app.services.experiment_registry import ManifestError, status

    try:
        out = status(experiment_id, _Path(base) if base else None)
    except Exception as exc:
        print(f"error: {exc}")
        return 2
    if fmt == "json":
        print(_json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0
    print(f"experiment {out['experiment_id']}")
    for k in ("state", "kind", "class", "domain", "primary_metric", "sample_floor",
              "start_time", "registered_at", "registration_commit", "events",
              "evaluation_permitted"):
        print(f"  {k:22} {out[k]}")
    print(f"  allowed_transitions    {out['allowed_transitions']}")
    print(f"  results                {out['results']}")
    i = out["integrity"]
    print(f"  manifest_intact        {i['intact']}  (digest {i['digest_recomputed_now']})")
    return 0 if i["intact"] else 1


def experiment_registry_list(base: str | None = None, fmt: str = "text") -> int:
    import json as _json

    from app.services.experiment_registry import DISCLAIMER, list_experiments

    try:
        rows = list_experiments(_Path(base) if base else None)
    except Exception as exc:
        print(f"error: registry is unreadable ({type(exc).__name__}: {exc})")
        print("this is exactly the state to investigate — see git history for "
              "experiments/")
        return 2
    if fmt == "json":
        print(_json.dumps({"experiments": rows, "disclaimer": DISCLAIMER},
                          indent=2, sort_keys=True, default=str))
        return 0
    print("prospective experiment registry")
    print(DISCLAIMER)
    if not rows:
        print("  (none registered)")
        return 0
    print(f"  {'id':34} {'state':12} {'kind':13} {'domain':16} intact")
    for r in rows:
        print(f"  {str(r['experiment_id']):34} {str(r['state']):12} "
              f"{str(r['kind']):13} {str(r['domain']):16} {r['intact']}")
    return 0 if all(r["intact"] for r in rows) else 1


def experiment_registry_transition(
    experiment_id: str, to: str, confirm: bool = False, base: str | None = None,
    note: str | None = None, fmt: str = "text",
) -> int:
    import json as _json

    from app.services.experiment_registry import ManifestError, transition

    try:
        out = transition(experiment_id, to, base=_Path(base) if base else None,
                         confirm=confirm, note=note)
    except ManifestError as exc:
        print(f"REFUSED: {exc}")
        return 1
    if fmt == "json":
        print(_json.dumps(out, indent=2, sort_keys=True, default=str))
        return 0
    print(f"experiment {out['experiment_id']} — {out['mode'].upper()}")
    print(f"  {out['from']} -> {out['to']}")
    print(f"  persisted {str(out['persisted']).lower()}")
    if not confirm:
        print("  nothing written — re-run with --confirm to apply")
    return 0


def outcome_sync_coverage_report(
    hours: int | None = None, since: str | None = None, until: str | None = None,
    domain: str | None = None, examples: int = 3, fmt: str = "text", session=None,
) -> int:
    """OUTCOME-SYNC-COVERAGE-001 — read-only outcome-coverage diagnostics.

    Zero provider calls, zero DB writes, no outcome sync, no scoring; never
    advice. Returns 0 ok / 2 on an invalid window.
    """
    import json as _json
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    from app.services.outcome_coverage import build_coverage_report

    def _parse(ts):
        if ts is None:
            return None
        d = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)

    now = _dt.now(_tz.utc)
    start = _parse(since)
    if start is None and hours is not None:
        if hours <= 0:
            print("error: --hours must be positive")
            return 2
        start = now - _td(hours=hours)
    end = _parse(until) or now
    if start is not None and start > end:
        print("error: window start is after window end")
        return 2

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_coverage_report(
            session, now=now, since=start, until=end, domain=domain,
            examples_per_reason=max(1, examples)).to_dict()
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return 0

    f = r["funnel"]
    print("outcome-sync coverage — measurement/data-quality only, never advice")
    print(r["disclaimer"])
    print(f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()} "
          f"settlement_grace={r['grace_seconds']}s")
    print()
    print("denominator funnel (forecast-level, monotonic):")
    for step in ("all_forecasts", "markets_closed", "matured_eligible",
                 "outcome_row_present", "settled_yes_no", "scored_current",
                 "unscorable", "pending_legitimately_unresolved", "missing_outcome"):
        print(f"  {step:34} {f[step]:>7}")
    print(f"  {'matured coverage':34} {f['matured_coverage_pct']:>6}%")
    print(f"  {'scored_current of matured':34} {f['scored_current_pct']:>6}%")

    print()
    print("missing-outcome taxonomy (mutually exclusive):")
    print(f"  {'reason':34} {'fcst':>6} {'mkts':>6} {'share':>7}  recoverability")
    for t in r["taxonomy"]:
        print(f"  {t['reason']:34} {t['forecasts']:>6} {t['markets']:>6} "
              f"{t['share_of_matured_pct']:>6}%  {t['recoverability']}"
              f"{' (provider call)' if t['provider_call_required'] else ''}")

    print()
    print("recoverability:")
    for k, v in sorted(r["recoverability"].items(), key=lambda kv: -kv[1]):
        print(f"  {str(k):36} {v:>7}")

    print()
    sel = r["selection"]
    print("outcome-sync selection audit:")
    print(f"  distinct forecasted tickers  {sel['distinct_forecasted_tickers']:>7}")
    print(f"  configured limit             {sel['configured_limit']:>7}")
    print(f"  reachable                    {sel['reachable_tickers']:>7}")
    print(f"  UNREACHABLE every cycle      {sel['unreachable_tickers']:>7}")
    print(f"  candidate pool               {sel['candidate_pool']:>7}")
    if sel.get("full_sweep_cycles") is not None:
        print(f"  full sweep                   {sel['full_sweep_cycles']:>7} cycles "
              f"(~{sel['full_sweep_hours']} h)")
    print(f"  active selection             {sel['active_selection']:>7}"
          f"  repair_enabled={sel['repair_enabled']}")
    print(f"  terminal rows re-fetched     {sel['terminal_rows_reselected']:>7}")
    print(f"  {sel['verdict']}")
    print(f"  {sel['detail']}")

    print()
    print("coverage by domain:")
    for b in r["by_domain"]:
        print(f"  {b['key']:20} matured={b['matured']:>6} usable={b['usable']:>6} "
              f"scored={b['scored_current']:>6} coverage={b['coverage_pct']:>6}%")
    print("coverage by close age:")
    for b in r["by_close_age"]:
        print(f"  {b['key']:10} matured={b['matured']:>6} usable={b['usable']:>6} "
              f"coverage={b['coverage_pct']:>6}%")
    print("coverage by forecaster (top 8):")
    for b in r["by_forecaster"][:8]:
        print(f"  {b['key']:28} matured={b['matured']:>6} coverage={b['coverage_pct']:>6}%")

    u = r["uplift"]
    print()
    print("scoring uplift potential:")
    print(f"  coverage now                       {u['matured_coverage_now_pct']:>7}%")
    print(f"  recoverable with current providers {u['recoverable_with_current_providers']:>7}")
    print(f"  requires new mapping               {u['requires_new_mapping']:>7}")
    print(f"  requires new status interpreter    {u['requires_new_status_interpreter']:>7}")
    print(f"  requires NEW PROVIDER              {u['requires_new_provider']:>7}")
    print(f"  permanently unscorable             {u['permanently_unscorable']:>7}")
    print(f"  max attainable coverage            {u['max_attainable_coverage_pct']:>7}%  "
          "(LOOSE upper bound: assumes every recoverable market settles yes/no)")
    print("  max attainable, excluding markets  "
          f"{u['max_attainable_excluding_awaiting_settlement_pct']:>7}%  "
          "(TIGHT bound; the truth is between these two)")
    print("    merely awaiting settlement")

    if r["data_quality"]:
        print()
        print("data-quality findings:")
        for d in r["data_quality"]:
            print(f"  - {d}")
    print()
    print(f"VERDICT: {r['verdict']}")
    return 0


def outcome_sync_backfill(
    confirm: bool = False, max_markets: int = 250, domain: str | None = None,
    fmt: str = "text", session=None,
) -> int:
    """OUTCOME-SYNC-COVERAGE-001 — bounded historical outcome synchronization.

    Fetches market-resolution status for matured markets that currently have no
    usable outcome, through the SAME read-only Kalshi detail path MarketOps
    already uses. Without `--confirm` this is a dry run that calls nothing.

    Never changes a forecast, never infers an outcome from price, never
    overwrites a conflict, and stops at the cap.
    """
    import asyncio as _asyncio
    import json as _json

    from app.services.outcome_backfill import run_backfill

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        result = _asyncio.run(run_backfill(
            session, confirm=confirm, max_markets=max_markets, domain=domain))
    finally:
        if owns_session:
            session.close()

    data = result.to_dict()
    if fmt == "json":
        print(_json.dumps(data, indent=2, default=str))
    else:
        mode = "CONFIRMED RUN" if confirm else "DRY RUN"
        print(f"outcome-sync backfill — {mode}")
        print(data["disclaimer"])
        print(f"  markets_selected     {data['markets_selected']:>7}")
        print(f"  provider_cap         {data['provider_cap']:>7}")
        print(f"  provider_calls       {data['provider_calls']:>7}")
        print(f"  already_current_excluded {data['already_current_excluded']:>3}")
        print(f"  conflicts_excluded   {data['conflicts_excluded']:>7}")
        print(f"  scoring_candidates   {data['scoring_candidates']:>7}")
        print(f"  outcomes_created     {data['outcomes_created']:>7}")
        print(f"  outcomes_refreshed   {data['outcomes_refreshed']:>7}")
        print(f"  settled_yes_no       {data['settled_yes_no']:>7}")
        print(f"  canceled_or_void     {data['canceled_or_void']:>7}")
        print(f"  still_unsettled      {data['still_unsettled']:>7}")
        print(f"  unrecognized_status  {data['unrecognized_status']:>7}")
        print(f"  provider_failures    {data['provider_failures']:>7}")
        print("  scoring: not performed here — canonical scoring runs as its "
              "own MarketOps stage")
        print(f"  persisted={str(data['persisted']).lower()}  "
              f"stop_reason={data['stop_reason']}")
        if not confirm:
            print("  nothing written and nothing fetched — "
                  "re-run with --confirm to apply")
        for reason, n in sorted(data["by_reason"].items(), key=lambda kv: -kv[1]):
            print(f"    {reason:34} {n:>6}")
    # "dry_run" belongs here: the documented, default, SAFE invocation must not
    # report failure. It previously exited 1, so a runbook under `set -e` would
    # fail on the safe step and the operator's next move would be --confirm.
    if data["stop_reason"] in ("dry_run", "completed", "provider_cap", "max_markets"):
        return 0
    return 1


async def forecast_scorability_audit_report(
    hours: int | None = None, since: str | None = None, until: str | None = None,
    domain: str | None = None, forecaster: str | None = None, top: int = 10,
    fmt: str = "text", session=None,
) -> int:
    """FORECAST-SCORABILITY-AUDIT-001 — read-only scorability + representativeness
    diagnostics over persisted forecasts. Zero provider calls, zero DB writes, no
    outcome sync, no scoring; never advice. Returns 0 ok / 2 on invalid window."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    from app.services.forecast_scorability import build_scorability_report

    def _parse(ts):
        if ts is None:
            return None
        d = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_scorability_report(
            session, hours=hours, since=_parse(since), until=_parse(until),
            domain=domain, forecaster=forecaster, top=max(1, top))
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return 0
    c = r["counts"]
    print("forecast scorability audit — measurement/data-quality only, never advice")
    print(r["disclaimer"])
    print(f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()} "
          f"writes={r['writes']}")
    w = r["window"]
    print(f"window: since={w['since_utc']} until={w['until_utc']} hours={w['hours']} "
          f"domain={w['domain_filter']} forecaster={w['forecaster_filter']}")
    if w["domain_filtered_out"]:
        print(f"  (truncated: {w['domain_filtered_out']} forecasts filtered out by --domain)")
    print(f"counts: forecasts={c['forecasts']} matured_eligible={c['matured_eligible']} "
          f"scored_current={c['scored_current']} pending={c['legitimately_pending']} "
          f"unscorable={c['unscorable']} scorable_backlog={c['scorable_backlog']} "
          f"stale_backlog={c['stale_score_backlog']} inconsistent={c['inconsistent']}")
    print("scorability funnel:")
    for s in r["scorability_funnel"]["scorability_steps"]:
        print(f"  {s['step']:34} {s['count']:5}  {s['pct_of_all']:5.1f}%")
    print(f"state histogram: {r['state_histogram']}")
    print("cohorts (domain):")
    for co in r["cohorts"]["domain"]:
        print(f"  {co['name']:20} n={co['total']:4} scored={co['scored_current']:4} "
              f"rate={co['scorability_rate']} conc={co['concentration_share_of_scored']} "
              f"{co['sample_label']}")
    print("representation (domain):")
    for rp in r["representation"]["domain"]:
        print(f"  {rp['name']:20} all%={rp['share_all_pct']:5} scored%={rp['share_scored_pct']:5} "
              f"delta_pp={rp['representation_delta_pp']:6} {rp['label']}")
    print("latency (seconds):")
    for k, v in r["latency"].items():
        print(f"  {k:24} n={v['count']:4} median={v['median_s']} p90={v['p90_s']} "
              f"max={v['max_s']} missing={v['missing']} neg={v['negative_findings']}")
    print(f"VERDICT: {r['verdict']['primary']}  blockers={r['verdict']['blockers']}")
    print(f"  rates={r['verdict']['rates']}")
    return 0


async def forecast_reliability_decomposition_report(
    hours: int | None = None, since: str | None = None, until: str | None = None,
    domain: str | None = None, forecaster: str | None = None, bins: int = 10,
    minimum_bin_count: int | None = None, minimum_cohort_count: int | None = None,
    top: int = 10, fmt: str = "text", session=None,
) -> int:
    """FORECAST-RELIABILITY-DECOMP-001 — read-only calibration/reliability
    decomposition over scored_current forecasts only (reuses the scorability
    population). Zero calls, zero writes; never advice. 0 ok / 2 invalid input."""
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    from app.services.forecast_reliability import build_reliability_report

    def _parse(ts):
        if ts is None:
            return None
        d = _dt.fromisoformat(ts.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=_tz.utc)

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_reliability_report(
            session, hours=hours, since=_parse(since), until=_parse(until),
            domain=domain, forecaster=forecaster, bins=bins,
            minimum_bin_count=minimum_bin_count, minimum_cohort_count=minimum_cohort_count,
            top=max(1, top))
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return 0
    p = r["population"]
    b = r["baselines"]
    m = r["murphy_decomposition"]
    print("forecast reliability decomposition — measurement only, never advice")
    print(r["disclaimer"])
    print(f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()} "
          f"writes={r['writes']}")
    w = r["window"]
    print(f"window: since={w['since_utc']} until={w['until_utc']} hours={w['hours']} bins={w['bins']} "
          f"domain={w['domain_filter']} forecaster={w['forecaster_filter']}")
    if w["domain_filtered_out"]:
        print(f"  (truncated: {w['domain_filtered_out']} forecasts filtered out by --domain)")
    print(f"population: all={p['all_forecasts']} scored_current={p['scored_current']} "
          f"excl_pending={p['excluded_pending']} excl_unscorable={p['excluded_unscorable']} "
          f"excl_backlog={p['excluded_backlog']} excl_stale={p['excluded_stale']} "
          f"excl_inconsistent={p['excluded_inconsistent']}")
    print("reliability bins [lo,hi) (last [lo,hi]):")
    for bs in r["reliability_bins"]:
        if bs["count"] == 0:
            continue
        print(f"  [{bs['lower']:.2f},{bs['upper']:.2f}] n={bs['count']:4} "
              f"mean_p={bs['mean_forecast_probability']} obs={bs['observed_positive_rate']} "
              f"gap={bs['calibration_gap']} brier={bs['mean_brier']} {bs['direction']} {bs['label']}")
    ce = r["calibration_error"]
    print(f"calibration error: ECE={ce['ece']} MCE={ce['mce']} "
          f"populated={ce['populated_bins']} measured={ce['measured_bins']} thin={ce['too_thin_bins']}")
    print(f"baselines: prevalence={b['prevalence']} model_brier={b['model_brier']} "
          f"neutral={b['neutral_baseline_brier']} base_rate={b['base_rate_baseline_brier']} "
          f"skill_vs_base_rate={b['brier_skill_vs_base_rate']} skill_vs_neutral={b['brier_skill_vs_neutral']}")
    print(f"murphy: reliability={m['reliability']} resolution={m['resolution']} "
          f"uncertainty={m['uncertainty']} reconstructed={m['reconstructed_brier']} "
          f"actual={m['actual_brier']} residual={m['discretization_residual']} "
          f"populated_bins={m['populated_bins']}")
    d = r["directional"]
    print(f"directional (weighted by {d['directional_weight_basis']}, "
          f"tolerance={d['calibration_tolerance']}):")
    print(f"  signed_aggregate_gap={d['signed_calibration_gap']}  "
          f"(mean_p - base_rate; NOT the complement of the shares below)")
    print(f"  over_share={d['overprediction_weighted_share']} "
          f"under_share={d['underprediction_weighted_share']} "
          f"approx_calibrated_share={d['approximately_calibrated_weighted_share']} "
          f"unclassified={d['unclassified_weighted_share']}")
    print(f"  extreme_miss={d['extreme_confidence_miss_count']} "
          f"high_correct={d['high_confidence_correct_count']}")
    print("cohorts (domain):")
    for c in r["cohorts"]["domain"]:
        print(f"  {c['name']:20} n={c['scored_count']:4} prev={c['prevalence']} brier={c['mean_brier']} "
              f"skill={c['brier_skill_vs_base_rate']} ECE={c['ece']} MCE={c['mce']} {c['sample_label']}")
    t = r["temporal"]["trend"]
    print(f"temporal trend: {t['label']} (measured_periods={t['measured_periods']} "
          f"early_ece={t.get('early_mean_ece')} late_ece={t.get('late_mean_ece')})")
    print(f"VERDICT: {r['verdict']['primary']}  findings={r['verdict']['findings']}")
    return 0


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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()

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


def raw_payload_reclaim(
    target: str, confirm: bool = False, max_rows: int | None = None,
    fmt: str = "text", session=None
) -> dict:
    """RAW-PAYLOAD-RECLAMATION-001 — the ONE mutating command.

    Replaces eligible historical raw bodies with the canonical suppressed
    envelope, in bounded verified batches. `--confirm` is required; without it
    this is a dry run. `target` must name a reviewed registry entry — there is
    no arbitrary table/column interface, because a typo on an irreversible
    operation is not a recoverable mistake.

    Never deletes a row, never touches a normalized column or a timestamp, never
    runs VACUUM, never changes a pragma, and never shrinks the file.
    """
    import json as _json

    from app.services.raw_payload_reclamation import (
        build_report,
        reclaim_target,
        resolve_target,
    )

    try:
        resolved = resolve_target(target)
    except LookupError as exc:
        # A refused target is an operator typo, not a crash. Print the reason
        # and the valid names rather than a traceback.
        from app.services.raw_payload_reclamation import RECLAMATION_TARGETS

        print(f"refused: {exc}")
        print(f"valid targets: {', '.join(sorted(RECLAMATION_TARGETS))}")
        return {"error": str(exc), "refused": True}
    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
    try:
        if not confirm:
            data = build_report(session, targets=[resolved.qualified]).to_dict()
            data["mode"] = "dry_run"
            if fmt == "json":
                print(_json.dumps(data, indent=2, sort_keys=True, default=str))
            else:
                plan = data["targets"][0]
                print(f"DRY RUN {resolved.qualified}")
                print(f"  backup: {'OK' if data['backup_ok'] else 'BLOCKED'} — "
                      f"{data['backup_detail']}")
                print(f"  eligible_rows={plan.get('eligible_rows')} "
                      f"eligible_MiB={(plan.get('eligible_bytes') or 0)/1048576:.1f} "
                      f"net_MiB={(plan.get('estimated_reclaimed_bytes') or 0)/1048576:.1f}")
                print(f"  effective_cutoff={str(plan.get('effective_cutoff'))[:19]}")
                print("  nothing written — re-run with --confirm to apply")
            return data
        result = reclaim_target(session, resolved, max_rows=max_rows).to_dict()
    finally:
        if owns_session:
            session.close()

    result["mode"] = "confirmed"
    if fmt == "json":
        print(_json.dumps(result, indent=2, sort_keys=True, default=str))
        return result
    print(f"CONFIRMED reclamation of {result['target']}")
    print(f"  started={result['started_at']}  finished={result['finished_at']}")
    print(f"  batches attempted={result['batches_attempted']} "
          f"committed={result['batches_committed']}")
    print(f"  rows_changed={result['rows_changed']}  lock_retries={result['lock_retries']}")
    print(f"  bytes before={result['bytes_before']} after={result['bytes_after']}  "
          f"logical reclaimed={(result['bytes_before']-result['bytes_after'])/1048576:.1f} MiB")
    print(f"  total_seconds={result['total_seconds']}  verified={result['verified']}")
    print(f"  stop_reason={result['stop_reason']}")
    print()
    print("  NOTE: reclaimed bytes are now REUSABLE FREELIST PAGES. The SQLite "
          "file did NOT shrink and")
    print("  db_growth_warning stays critical. Only SQLITE-COMPACT-COPY-001 "
          "changes the file size.")
    return result


def raw_payload_reclamation_report(
    fmt: str = "text", session=None
) -> dict:
    """RAW-PAYLOAD-RECLAMATION-001 — read-only dry-run for historical reclamation.

    Reports, per reviewed target: the preservation floor and why, the effective
    cutoff, exactly how many historical rows are eligible, how many bytes they
    hold, what the envelope would cost, and what that nets out to. Also reports
    every row it is NOT touching and why.

    Writes nothing, calls no provider, and never reads a payload's contents —
    only `length()`. There is no `--confirm` on this command; the mutation lives
    behind a separate one.
    """
    import json as _json

    from app.services.raw_payload_reclamation import build_report

    owns_session = session is None
    if owns_session:
        # No run_migrations(): a read-only report must not apply Alembic to
        # whatever DATABASE_URL points at.
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
    try:
        report = build_report(session)
    finally:
        if owns_session:
            session.close()

    data = report.to_dict()
    if fmt == "json":
        print(_json.dumps(data, indent=2, sort_keys=True, default=str))
        return data

    mib = lambda b: "-" if b is None else f"{b / 1048576:.1f}"
    print("raw payload historical reclamation — DRY RUN (writes nothing, zero "
          "provider calls, prints no payload)")
    print(f"generated_at={data['generated_at']}  external_calls=0  "
          f"persisted=false  rows_changed=0")
    print()
    print(f"backup prerequisite: {'OK' if data['backup_ok'] else 'BLOCKED'} — "
          f"{data['backup_detail']}")
    print()
    header = (f"{'target':<48}{'rows':>9}{'eligible':>9}{'MiB now':>9}"
              f"{'MiB env':>9}{'MiB net':>9}{'kept':>8}{'suppr':>8}")
    print(header)
    print("-" * len(header))
    total_net = 0
    for row in data["targets"]:
        total_net += row["estimated_reclaimed_bytes"]
        if row.get("error"):
            print(f"{row['target']:<48}  ERROR: {row['error']}")
            continue
        print(f"{row['target']:<48}{row['total_rows']:>9}{row['eligible_rows']:>9}"
              f"{mib(row['eligible_bytes']):>9}"
              f"{mib(row['estimated_replacement_bytes']):>9}"
              f"{mib(row['estimated_reclaimed_bytes']):>9}"
              f"{row['preserved_recent_rows']:>8}"
              f"{row['already_suppressed_rows']:>8}")
    print("-" * len(header))
    print(f"{'TOTAL logical bytes reclaimable':<48}{'':>9}{'':>9}{'':>9}{'':>9}"
          f"{mib(total_net):>9}")
    print()
    print("preservation floors (the EARLIER of the two cutoffs wins):")
    for row in data["targets"]:
        if row.get("error"):
            continue
        print(f"  {row['target']}")
        print(f"      age floor      {row['preservation_days']}d -> {row['cutoff'][:19]}")
        print(f"      backup floor   latest verified backup -> {row['backup_cutoff'][:19]}")
        print(f"      EFFECTIVE      {row['effective_cutoff'][:19]}")
        print(f"      why: {row['rationale']}")
    print()
    print("excluded from reclamation (governed, but deliberately not a target):")
    for column, why in data["excluded"].items():
        print(f"  {column}\n      {why}")
    print()
    print(f"pinned columns (production readers — never reclaimable): "
          f"{len(data['pinned'])}")
    for column in data["pinned"]:
        print(f"  {column}")
    print()
    print("BACKUP PREREQUISITE: reclamation is irreversible. A verified backup "
          "newer than 12h is required,")
    print("and rows newer than that backup are never eligible — they would have "
          "no recovery path at all.")
    print()
    print("LOGICAL vs PHYSICAL: reclaimed bytes become REUSABLE FREELIST PAGES. "
          "The SQLite file does NOT")
    print("shrink, and db_growth_warning stays critical. Only "
          "SQLITE-COMPACT-COPY-001 changes the file size,")
    print("and it is not authorized here.")
    return data


def raw_payload_storage_report(
    fmt: str = "text", recent: int = 5000, full_scan: bool = False, session=None
) -> dict:
    """RAW-PAYLOAD-STORAGE-001 — read-only raw-payload storage measurement.

    Reports the configured capture mode and, per governed column, how many of
    the NEWEST rows carry a full body versus a suppressed provenance envelope,
    and the bytes those rows store.

    Writes nothing, calls no provider, and NEVER prints payload contents — only
    counts, sizes and the column names themselves.

    **The window is the newest N rows by primary key, not a time range.** A time
    range would be the natural framing, but none of these tables has an index on
    its timestamp column, so `WHERE created_at >= ...` is a full table scan —
    against `market_price_ticks` that is ~900 MiB of page reads including
    `sum(length(raw_payload))`, holding a SHARED lock under
    `journal_mode=delete` while a 60s watcher and a 5-minute MarketOps are
    writing. That is exactly the hazard SQLITE-BACKUP-COORDINATION-001 exists to
    avoid. A descending primary-key scan with a LIMIT is index-ordered and
    bounded, and it answers the activation question directly: are the NEWEST
    rows envelopes?

    PINNED columns are listed without being queried — the capture mode can never
    touch them, so scanning them buys no decision value.

    `--full-scan` adds whole-table aggregates. It reads every page of every
    payload column; use it against a decompressed backup copy, never the live
    database.

    It deliberately does NOT call run_migrations(), unlike most commands here: a
    report that claims "writes nothing" must not apply Alembic migrations to
    whatever DATABASE_URL points at, and --full-scan explicitly points it at a
    backup snapshot that must stay untouched evidence.
    """
    import json as _json
    from datetime import datetime, timezone

    from sqlalchemy import String as _String
    from sqlalchemy import and_
    from sqlalchemy import func as _f
    from sqlalchemy import select as _select

    from app.db import Base
    from app.services.raw_payload_policy import (
        GOVERNED_COLUMNS,
        PINNED_FULL,
        SUPPRESSED_MARKER,
        resolve_capture_mode,
    )

    owns_session = session is None
    if owns_session:
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
    try:
        mode = resolve_capture_mode()
        now = datetime.now(timezone.utc)
        recent = max(1, recent)
        marker = f'%"{SUPPRESSED_MARKER}"%'
        rows_out = []
        for qualified in sorted(set(GOVERNED_COLUMNS) | set(PINNED_FULL)):
            table_name, column = qualified.split(".", 1)
            table = Base.metadata.tables.get(table_name)
            if table is None or column not in table.c:
                continue
            pinned = qualified in PINNED_FULL
            col = table.c[column]
            total = session.execute(
                _select(_f.count()).select_from(table)
            ).scalar() or 0

            recent_rows = recent_full = recent_suppressed = None
            recent_bytes = None
            if not pinned:
                pk = list(table.primary_key.columns)[0]
                window = (
                    _select(pk.label("pk"), col.label("payload"))
                    .order_by(pk.desc()).limit(recent).subquery()
                )
                # SQLAlchemy's JSON type stores Python None as the JSON
                # literal `null`, NOT as SQL NULL, so `IS NOT NULL` alone counts
                # a genuinely absent payload as present. Both forms must be
                # excluded or the operator's primary verification signal — "are
                # new rows envelopes?" — never reaches its floor.
                has_payload = and_(
                    window.c.payload.is_not(None),
                    _f.cast(window.c.payload, _String) != "null",
                )
                recent_rows = session.execute(
                    _select(_f.count()).select_from(window).where(has_payload)
                ).scalar() or 0
                recent_suppressed = session.execute(
                    _select(_f.count()).select_from(window)
                    .where(window.c.payload.like(marker))
                ).scalar() or 0
                recent_bytes = session.execute(
                    _select(_f.coalesce(_f.sum(_f.length(window.c.payload)), 0))
                    .select_from(window).where(has_payload)
                ).scalar() or 0
                # NULL means the provider gave nothing — it is neither a full
                # body nor a suppression, and counting it as "full" would mean
                # the operator's primary verification signal never reaches 0.
                recent_full = max(recent_rows - recent_suppressed, 0)

            non_null = stored_bytes = suppressed = full_rows = None
            avg_full = None
            if full_scan:
                non_null = session.execute(
                    _select(_f.count()).select_from(table).where(col.is_not(None))
                ).scalar() or 0
                stored_bytes = session.execute(
                    _select(_f.coalesce(_f.sum(_f.length(col)), 0)).select_from(table)
                ).scalar() or 0
                suppressed = session.execute(
                    _select(_f.count()).select_from(table).where(col.like(marker))
                ).scalar() or 0
                full_rows = max(non_null - suppressed, 0)
                full_bytes = session.execute(
                    _select(_f.coalesce(_f.sum(_f.length(col)), 0))
                    .select_from(table)
                    .where(col.is_not(None), _f.coalesce(col, "").notlike(marker))
                ).scalar() or 0
                avg_full = round(full_bytes / full_rows, 1) if full_rows else None

            rows_out.append({
                "column": qualified,
                "pinned_full": pinned,
                "rows_total": total,
                "recent_window": None if pinned else recent,
                "recent_rows_with_payload": recent_rows,
                "recent_rows_full_payload": recent_full,
                "recent_rows_suppressed": recent_suppressed,
                "recent_bytes": recent_bytes,
                "rows_non_null": non_null,
                "rows_full_payload": full_rows,
                "rows_suppressed": suppressed,
                "stored_bytes": stored_bytes,
                "avg_bytes_per_full_row": avg_full,
            })
    finally:
        if owns_session:
            session.close()

    data = {
        "generated_at": now.isoformat(),
        "capture_mode": mode,
        "recent_window": recent,
        "full_scan": full_scan,
        "columns": rows_out,
        "governed_columns": sorted(GOVERNED_COLUMNS),
        "pinned_columns": sorted(PINNED_FULL),
        "external_calls": 0,
        "persisted": False,
        "prints_payload_contents": False,
    }
    if fmt == "json":
        print(_json.dumps(data, indent=2, sort_keys=True, default=str))
        return data

    mib = lambda b: "-" if b is None else f"{b / 1048576:.2f}"
    dash = lambda v: "-" if v is None else v
    print("raw payload storage — READ ONLY (writes nothing, zero provider calls, "
          "never prints payload contents)")
    print(f"generated_at={data['generated_at']}  reporting-process "
          f"capture_mode={mode}  newest={recent} rows/column  "
          f"external_calls=0  persisted=false")
    print()
    header = (f"{'column':<50}{'rows':>10}{'new w/pl':>10}{'new full':>10}"
              f"{'new supp':>10}{'new MiB':>9}{'all MiB':>9}  pin")
    print(header)
    print("-" * len(header))
    for row in data["columns"]:
        print(f"{row['column']:<50}{row['rows_total']:>10}"
              f"{dash(row['recent_rows_with_payload']):>10}"
              f"{dash(row['recent_rows_full_payload']):>10}"
              f"{dash(row['recent_rows_suppressed']):>10}"
              f"{mib(row['recent_bytes']):>9}"
              f"{mib(row['stored_bytes']):>9}"
              f"  {'PINNED' if row['pinned_full'] else ''}")
    print()
    print("PINNED columns are listed but never queried — the capture mode "
          "cannot touch them.")
    if not full_scan:
        print("Whole-table columns blank by design: --full-scan reads every page "
              "of every payload")
        print("column and would hold a read lock that blocks the writer. Use it "
              "on a backup copy.")
    print()
    print("The capture_mode above is THIS process's. A long-lived writer (the "
          "watcher service) keeps the")
    print("mode it started with — @lru_cache'd settings — so after changing "
          ".env it must be restarted")
    print("before market_price_ticks reflects the new mode. See "
          "docs/RAW_PAYLOAD_STORAGE_001.md section 11.")
    print()
    print("LOGICAL vs PHYSICAL: suppression avoids FUTURE writes. It does not "
          "delete history and does not")
    print("shrink the SQLite file — freed space becomes reusable pages only "
          "after separate reclamation")
    print("(RAW-PAYLOAD-RECLAMATION-001) and compaction (SQLITE-COMPACT-COPY-001). "
          "The database-growth")
    print("alert stays critical until compaction; only that milestone can clear it.")
    return data


def retention_coverage_report(
    fmt: str = "text", top: int = 25, no_dbstat: bool = False, session=None
) -> dict:
    """RETENTION-COVERAGE-001 — read-only retention coverage analysis.

    Reports, per table: size, growth, current retention window, classification,
    readers, preservation floor, and how many rows are eligible under the
    CURRENT policy. It deletes nothing, writes nothing, calls no provider, and
    has deliberately NO --confirm path: this milestone produces a decision
    package, it does not enact one.

    `--no-dbstat` skips per-table page attribution. Prefer it on a large
    production database: the dbstat scan walks every page, and a long read lock
    under journal_mode=delete can block the concurrent writer's COMMIT. The
    zero-risk way to get byte attribution is to run this against a decompressed
    backup snapshot instead.
    """
    import json as _json

    from app.services.retention_coverage import build_retention_coverage

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        report = build_retention_coverage(session, include_dbstat=not no_dbstat)
    finally:
        if owns_session:
            session.close()

    data = report.to_dict()
    if fmt == "json":
        print(_json.dumps(data, indent=2, sort_keys=True, default=str))
        return data

    mib = lambda b: ("-" if b is None else f"{b / 1048576:.1f}")
    print("retention coverage — READ ONLY (deletes nothing, writes nothing, "
          "zero provider calls)")
    print(f"generated_at={data['generated_at']}")
    print(f"database: {mib(data['database_bytes'])} MiB  "
          f"warning={data['warning_mb']:.0f} MiB  critical={data['critical_mb']:.0f} MiB  "
          f"over_critical_by={data['over_critical_by_mb']} MiB")
    print(f"pages: count={data['page_count']} size={data['page_size']} "
          f"freelist={data['freelist_pages']} "
          f"({mib(data['freelist_bytes'])} MiB, {data['freelist_percent']}%)")
    print(f"dbstat_available={str(data['dbstat_available']).lower()}  "
          f"external_calls={data['external_calls']}  "
          f"persisted={str(data['persisted']).lower()}  "
          f"deletes_performed={data['deletes_performed']}")
    print()
    header = (f"{'table':<38}{'rows':>10}{'MiB':>9}{'%db':>7}{'B/row':>8}"
              f"{'window':>8}{'eligible':>10}  classification")
    print(header)
    print("-" * len(header))
    for row in data["tables"][:top]:
        window = "never" if row["retention_days"] is None else f"{row['retention_days']}d"
        print(f"{row['table']:<38}{row['rows']:>10}{mib(row['total_bytes']):>9}"
              f"{('-' if row['percent_of_database'] is None else row['percent_of_database']):>7}"
              f"{('-' if row['bytes_per_row'] is None else int(row['bytes_per_row'])):>8}"
              f"{window:>8}"
              f"{('-' if row['eligible_now'] is None else row['eligible_now']):>10}"
              f"  {row['classification']}")
    print()
    print("preservation floors (evidence-backed minimums, not size-driven):")
    for row in data["tables"]:
        if row["preservation_floor_reason"]:
            floor = ("indefinite" if row["preservation_floor_days"] is None
                     else f"{row['preservation_floor_days']}d")
            print(f"  {row['table']:<38} {floor:>10}  {row['preservation_floor_reason']}")
    print()
    blocked = [r for r in data["tables"]
               if r["classification"] == "blocked_pending_dependency_review"]
    print(f"blocked pending dependency review: {len(blocked)}")
    for row in blocked[:12]:
        print(f"  {row['table']:<38} readers={row['readers'] or ['UNTRACED']}")
    print()
    print("LOGICAL vs PHYSICAL: every 'eligible' figure above is rows, and the "
          "bytes they free become REUSABLE FREELIST PAGES.")
    print("The SQLite file is a high-water-mark ratchet — deleting rows does "
          "NOT shrink it. Reclaiming file bytes")
    print("requires a separately-approved compaction (VACUUM / VACUUM INTO). "
          "This command authorizes neither.")
    return data


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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


def _resolve_db_target(url_str: str):
    """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R2 — the guard
    `db_schema_report`/`db_integrity_check` CLAIMED to have and did not.

    Both docstrings promised to FAIL if the resolved path is "empty or
    unexpected", but the check only asserted that the URL string was
    non-empty and carried a path COMPONENT. Pointed at a path that does not
    exist, `db-integrity-check` therefore printed
    `integrity_check duration: 0.00s / ok / PASS`, exited 0, and left a
    0-byte SQLite database behind — it CREATED the very file it claimed to
    have inspected and then certified the empty result as green. That is
    the identical defect class as the original `sqlite3 ""` HIGH-2 this
    command was written to replace: a green result carrying zero diagnostic
    content. It matters most in the two places the command exists for — the
    4.55 GB production migration gate, and the restore flow, where pointing
    at a not-yet-restored target is a realistic operator mistake and a
    false PASS is worse than no check at all.

    Returns `(resolved_display, sqlite_path_or_None, failure_message_or_None)`.
    Non-SQLite backends skip the file checks (there is no local file to
    stat); the URL-shape checks still apply.
    """
    from pathlib import Path

    from sqlalchemy.engine.url import make_url

    url = make_url(url_str)
    resolved = url.render_as_string(hide_password=True)
    if not url_str or not resolved:
        return resolved, None, "resolved database path is empty"
    if url.get_backend_name() != "sqlite":
        return resolved, None, None
    if not url.database:
        return resolved, None, (
            "sqlite URL resolved with no file path (e.g. an in-memory or blank DSN)"
        )
    if url.database == ":memory:":
        return resolved, None, (
            "sqlite URL resolved to an in-memory database — there is no "
            "persistent file to inspect"
        )
    path = Path(url.database).expanduser()
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return resolved, path, (
            f"resolved database file does not exist: {path} — refusing to "
            "create it and report the empty result as a pass"
        )
    except OSError as exc:
        return resolved, path, f"cannot stat resolved database file {path}: {exc}"
    if size == 0:
        return resolved, path, (
            f"resolved database file is 0 bytes: {path} — an empty file "
            "trivially passes every check while proving nothing"
        )
    return resolved, path, None


async def db_schema_report() -> int:
    """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B7/B9 — a
    repository-owned revision report replacing `sqlite3 "$DB_PATH" ...`
    (undefined variable) in the operator runbook. Prints the RESOLVED
    database path (no secrets — password hidden by SQLAlchemy's own
    `render_as_string(hide_password=True)`) via THIS application's own
    resolution (`app.config.get_settings().database_url`), the database's
    stamped Alembic revision, the code's head revision, and the
    SCHEMA_MATCH/SCHEMA_BEHIND_CODE/SCHEMA_AHEAD_OF_CODE status
    (`app.db._schema_status` — see `ensure_schema_current`, B8). Read-only:
    never applies a migration. Returns 0 on SCHEMA_MATCH, 1 otherwise, so a
    scripted post-migration check can assert success without parsing text."""
    from sqlalchemy.engine.url import make_url

    from app.config import get_settings
    from app.db import (
        SCHEMA_MATCH,
        _alembic_config,
        _current_revision,
        _head_revision,
        _schema_status,
    )

    settings = get_settings()
    url_str = settings.database_url
    resolved, _path, failure = _resolve_db_target(url_str)
    print(f"resolved database path: {resolved}")
    # R2: stat the file BEFORE `_current_revision`, which opens a read-write
    # engine and would otherwise create a 0-byte database at a bad path and
    # then report `current revision: None` about it.
    if failure is not None:
        print(f"FAIL: {failure}")
        return 1

    config = _alembic_config(url_str)
    head = _head_revision(config)
    current = _current_revision(url_str)
    status = _schema_status(current, head, config)
    print(f"current revision: {current!r}")
    print(f"head revision:    {head!r}")
    print(f"status:           {status}")
    return 0 if status == SCHEMA_MATCH else 1


async def db_integrity_check() -> int:
    """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B7 — replaces
    `sqlite3 "" "PRAGMA integrity_check"` in the operator runbook, which
    opens a TEMPORARY, empty database (the `""` argument) and prints `ok`
    with exit 0 having inspected NOTHING — a green result with zero
    diagnostic content. Worse, the `sqlite3` binary is not installed on
    EVO-X2 at all (`ssh mikolabs which sqlite3` -> absent), so that
    command could never have run there. This uses the application's OWN
    resolved database (`app.config.get_settings().database_url`), prints the
    resolved path before running anything (so a caller can see exactly what
    was inspected), and — R2 — refuses to run at all unless that path
    resolves to a file that EXISTS and is NON-EMPTY, opening it READ-ONLY so
    it can never create or mutate what it inspects. See `_resolve_db_target`
    for why: the previous guard checked only the URL *string*, so pointing
    this at a nonexistent path created a 0-byte database and certified it
    `ok / PASS / exit 0`.

    DURATION (R0, corrected). This docstring used to claim "~7m18s at the
    current EVO-X2 database size". That figure was never measured — the
    milestone doc records it as carried forward from a review brief — and
    the repository's own on-host artifact refutes it:
    `docs/evidence/crypto-query-plan-and-denominator-recovery-001/analyze_live.json`
    records a session against the live 4 550 623 232-byte database whose
    TOTAL elapsed time (`started_at` -> `finished_at`) is **7.206 s**, and
    that session included a full `PRAGMA integrity_check` on the live file
    returning `ok`. A 438-second check cannot fit in a 7.2-second session;
    `7m18s` is almost certainly a unit transcription of ~7.2 SECONDS.

    The real cost is **~7 s warm** at 4.55 GB. It is strongly cache-bound:
    EVO holds the whole database resident (92 GB RAM, ~21 GB page cache) so
    it is always warm there, whereas on a memory-constrained host the same
    check degrades into random 4 KiB reads and takes orders of magnitude
    longer. That is a property of the measuring host, not of this command.
    See "Step 6: integrity without blocking writers" in
    `docs/EVO_X2_RUNBOOK.md`."""
    import time as _time

    from sqlalchemy import create_engine, text

    from app.config import get_settings
    from app.db import connect_args_for, get_engine

    settings = get_settings()
    url_str = settings.database_url
    resolved, path, failure = _resolve_db_target(url_str)
    print(f"resolved database path: {resolved}")
    if failure is not None:
        print(f"FAIL: {failure}")
        return 1

    # R2: open READ-ONLY. `get_engine()` opens read-write, which is what
    # allowed a nonexistent path to be created and then certified `ok`.
    # `mode=ro` makes that physically impossible rather than merely guarded
    # against — SQLite refuses to create the file at all (verified:
    # `sqlite3.OperationalError: unable to open database file`, and no file
    # appears). An integrity check has no business being able to write.
    # Same busy timeout as every other SQLite reader here (`connect_args_for`).
    disposable = None
    if path is not None:
        ro_url = f"sqlite:///file:{path}?mode=ro&uri=true"
        engine = disposable = create_engine(
            ro_url, connect_args=connect_args_for(url_str)
        )
    else:
        engine = get_engine()
    started = _time.monotonic()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("PRAGMA integrity_check")).fetchall()
    finally:
        duration_s = _time.monotonic() - started
        if disposable is not None:
            disposable.dispose()
    results = [r[0] for r in rows]
    print(f"integrity_check duration: {duration_s:.2f}s")
    for line in results:
        print(f"  {line}")
    ok = results == ["ok"]
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


async def db_growth_report(top: int = 12, session=None) -> int:
    """OPS-011 read-only DB growth/retention observability: size, table row
    counts + est MiB (dbstat when available), largest tables, tick age
    buckets, ticks-by-domain, edge-precheck/crypto row growth, backups,
    retention windows, and calibrated alert thresholds. Returns total rows."""
    from app.services.db_growth import build_growth_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        if r.tick_top_tickers:
            print("  heaviest tickers: " + ", ".join(
                f"{t[:26]}={n}" for t, n in r.tick_top_tickers[:5]))
        if r.tick_projected_steady_state_mib is not None:
            print(f"  projected steady-state under current retention: "
                  f"~{r.tick_projected_steady_state_mib} MiB "
                  f"({r.tick_est_daily_mib} MiB/day x {r.retention.tick_days}d window)")
        print(f"  aggregated buckets (OPS-012): {r.tick_bucket_total} rows "
              f"(telemetry summaries — never trading signals)")
        if r.above_critical:
            print("  !! DB size ABOVE CRITICAL threshold")
        elif r.above_warning:
            print("  ! DB size above warning threshold (below critical)")

        print(f"\nedge_precheck_snapshots: {r.edge_total} total"
              f"  (+{r.edge_last_hour}/h, +{r.edge_last_24h}/24h)")
        print(f"crypto_price_ticks: {r.crypto_tick_total} total (+{r.crypto_tick_last_hour}/h)"
              f"   crypto_token_risk_assessments: {r.crypto_risk_total} total")
        print(f"meme_attention_snapshots: {r.meme_attention_total} total "
              f"(+{r.meme_attention_last_hour}/h)   "
              f"meme_catalyst_events: {r.meme_catalyst_total} total")

        rc = r.retention
        print("\nretention windows: "
              f"ticks={rc.tick_days}d, tick_buckets={rc.tick_bucket_days}d, "
              f"crypto={rc.crypto_days}d, meme={rc.meme_days}d, "
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


async def aggregate_market_ticks(
    hours: int = 24,
    bucket_seconds: int | None = None,
    dry_run: bool = False,
    max_rows: int | None = None,
    subwindow_hours: int | None = None,
    scheduled: bool = False,
    session=None,
) -> int:
    """One bounded tick-aggregation pass (OPS-012, hardened by OPS-013): rolls
    raw market_price_ticks into fixed-interval OHLC/spread/liquidity buckets,
    COMMITTING PER SUB-WINDOW (default 1h) so the SQLite write lock is held for
    seconds, with bounded retry on a locked DB. Idempotent upsert; NEVER
    deletes or modifies raw ticks. --dry-run computes and reports without
    writing. With --scheduled it refuses unless ENABLE_TICK_AGGREGATION_TIMER=
    true (manual invocation always allowed). Storage/telemetry plumbing only —
    buckets are summaries, never trading signals; no EV/trade/sizing/orders/
    wallets/signing/execution. Returns buckets written, -1 on error/failed
    windows."""
    from app.config import get_settings
    from app.services.tick_aggregation import TickAggregationService

    if scheduled and not get_settings().enable_tick_aggregation_timer:
        print(
            "ENABLE_TICK_AGGREGATION_TIMER=false; scheduled tick-aggregation cycle "
            "skipped (set true in .env; manual runs are always allowed)"
        )
        return 0

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        stats = TickAggregationService().aggregate(
            session, hours=hours, bucket_seconds=bucket_seconds,
            dry_run=dry_run, max_rows=max_rows,
            subwindow_hours=subwindow_hours, scheduled=scheduled,
        )
        print(
            f"tick aggregation{' (DRY RUN — nothing written)' if stats.dry_run else ''}"
            f"{f' run #{stats.run_id}' if stats.run_id else ''}: "
            f"window={stats.window_start:%Y-%m-%dT%H:%M}Z..{stats.window_end:%Y-%m-%dT%H:%M}Z "
            f"bucket={stats.bucket_seconds}s subwindow={stats.subwindow_hours}h"
        )
        print(
            f"  rows_read={stats.rows_read} skipped_unusable={stats.rows_skipped_unusable} "
            f"buckets_written={stats.buckets_written} "
            f"(inserted={stats.buckets_inserted} updated={stats.buckets_updated}) "
            f"duration_ms={stats.duration_ms} max_commit_ms={stats.max_commit_ms}"
        )
        committed = [s for s in stats.subwindows if s.status == "ok"]
        if committed:
            print(
                f"  sub-windows committed={len(committed)} "
                f"(per-window commit p_max={max(s.commit_ms for s in committed)}ms, "
                f"retries_total={sum(s.commit_retries for s in committed)})"
            )
        if stats.truncated:
            print(
                f"  TRUNCATED at the row cap — aggregation complete only up to "
                f"{stats.covered_until}; rerun to continue (never silent)."
            )
        for iso in stats.oversized_windows:
            print(f"  !! OVERSIZED sub-window SKIPPED (loud): {iso} — rerun with a larger cap")
        for iso in stats.failed_windows:
            print(f"  !! COMMIT FAILED for sub-window {iso} — rolled back; rerun repairs it")
        print("  raw ticks unchanged — aggregation never deletes them (not advice; telemetry only)")
        return stats.buckets_written if not stats.failed_windows else -1
    finally:
        if owns_session:
            session.close()


async def tick_aggregation_report(session=None) -> int:
    """OPS-012 read-only aggregation coverage report: bucket totals/ranges,
    per-domain and per-interval counts, compression ratio, hour-level coverage
    of the recent raw window, retention windows, and the STAGED (not enacted)
    raw-retention recommendation. Changes nothing. Returns bucket count."""
    from app.services.tick_aggregation import TickAggregationReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = TickAggregationReportService().build(session)
        print("tick aggregation report — read-only storage telemetry, not advice")
        print(r.note)
        print(
            f"buckets={r.bucket_total} (oldest={r.bucket_oldest} newest={r.bucket_newest})  "
            f"raw_ticks={r.raw_total}"
        )
        print(f"buckets by domain:   {r.buckets_by_domain}")
        print(f"buckets by interval: {r.buckets_by_seconds}")
        if r.compression_ratio is not None:
            print(f"compression: ~{r.compression_ratio} raw ticks per bucket row")
        print(
            f"coverage (last 48h, hour granularity): "
            f"{r.covered_hours_last_48h}/{r.raw_hours_last_48h} raw-tick hours have buckets"
            + (f" (rate={r.coverage_rate_last_48h})" if r.coverage_rate_last_48h is not None else "")
            + f"  healthy={r.coverage_healthy}"
        )
        print(f"retention: {r.retention}")
        print(f"staged recommendation (NOT enacted): {r.staged_recommendation}")
        print(
            f"\nraw retention reduction READINESS (OPS-013 — evidence only, enacts nothing): "
            f"{r.readiness}"
        )
        print(
            f"  coverage_72h={r.coverage_rate_last_72h}  "
            f"clean_scheduled_cycles={r.clean_scheduled_cycles}  "
            f"recent_runs_with_errors={r.runs_with_errors_recent}  "
            f"raw_feed_fresh={r.raw_feed_fresh}"
        )
        if r.readiness_reasons:
            print(f"  not ready because: {r.readiness_reasons}")
        else:
            print(
                "  all gates pass — a raw-retention reduction (3d -> 24-48h) may be "
                "STAGED as a separate, explicitly accepted milestone. Nothing changes "
                "automatically."
            )
        if r.recent_runs:
            print("recent aggregation runs:")
            for x in r.recent_runs[:5]:
                print(
                    f"  #{x['id']} {x['status']}{' [scheduled]' if x['scheduled'] else ''} "
                    f"rows={x['rows_read']} buckets={x['buckets_written']} "
                    f"failed_windows={x['failed_windows']} truncated={x['truncated']}"
                )
        return r.bucket_total
    finally:
        if owns_session:
            session.close()


async def signals_recent(limit: int = 20, signal_status: str | None = None, session=None) -> int:
    """Print recent signals, newest first. Returns the number printed."""
    from app.services.signal_workflow import SignalPromotionService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


async def crypto_scan_once(
    limit: int | None = None,
    services=None,
    session=None,
    *,
    provider_plan: bool = False,
    allow_provider: list | None = None,
    deny_provider: list | None = None,
    confirm_paid_provider: list | None = None,
    yes: bool = False,
) -> int:
    """One read-only crypto discovery pass (Crypto Arena, CRYPTO-001), governed
    by an explicit provider policy (GATE-001). A bare invocation prints the
    zero-call provider plan and does NOT execute; execution requires --yes plus
    per-provider confirmation for any paid provider. Returns 0 on ok, 1 on error
    or a blocked/aborted plan."""
    from app.services.crypto_provider_policy import (
        provider_run,
        render_ledger,
        render_provider_plan,
        resolve_cli_policy,
    )
    from app.services.crypto_scout import CryptoDiscoveryService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        service = services or CryptoDiscoveryService()
        descriptors = service.describe_providers(limit)
        resolution = resolve_cli_policy(
            descriptors,
            allow=allow_provider or [],
            deny=deny_provider or [],
            confirm_paid=confirm_paid_provider or [],
        )
        # Bare invocation or explicit --provider-plan: print plan, zero calls.
        if provider_plan or not yes:
            print(render_provider_plan("crypto-scan-once", descriptors, resolution))
            if not yes:
                return 0 if resolution.policy is not None else 1
        if resolution.policy is None:
            # Fail closed: denied mandatory provider or unconfirmed paid provider.
            print(render_provider_plan("crypto-scan-once", descriptors, resolution))
            print("aborted: provider plan is blocked; no provider was contacted.")
            return 1
        with provider_run(resolution.policy, planned_max={
            d.provider: d.max_requests for d in descriptors if d.max_requests is not None
        }) as ctx:
            run = await service.scan_once(session, limit=limit, policy=resolution.policy)
            print(
                f"crypto scan #{run.id}: {run.status} tokens={run.tokens_checked} "
                f"pairs={run.pairs_checked} ticks={run.ticks_recorded} "
                f"signals={run.signals_created} in {run.duration_ms}ms"
            )
            print(render_ledger(ctx.ledger.snapshot()))
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


async def crypto_tape_run_once(
    limit: int | None = None, hours: int | None = None,
    dry_run: bool = False, session=None,
    source_crypto_run_id: int | None = None, confirm: bool = False,
) -> int:
    """CRYPTO-TAPE-001 lifecycle tape assembly: consolidate already-persisted
    surveillance rows into birth events, lifecycle snapshots, actor
    observations, and survival outcomes. Zero external calls; dry-run persists
    nothing. With --source-crypto-run-id (ANCHOR-FEED-MEASUREMENT-001), the
    pass consolidates EXACTLY the tokens first persisted by that discovery
    run — no freshest fallback, no substitution — and persists only under
    --confirm. Research infrastructure only — never advice. Returns tokens
    considered (negative on exact-run rejection)."""
    from app.services.crypto_tape import CryptoLifecycleTapeRecorder

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        if source_crypto_run_id is not None:
            if limit is not None or hours is not None:
                print(
                    "exact-run mode uses exact run membership: "
                    "--limit/--hours are rejected"
                )
                return -1
            from app.services.crypto_tape import new_token_ids_for_run

            recorder = CryptoLifecycleTapeRecorder()
            token_ids = new_token_ids_for_run(
                session, source_crypto_run_id, chain=recorder.config.chain
            )
            effective_dry = dry_run or not confirm
            r = recorder.record_discovery_run(
                session, source_crypto_run_id, token_ids, dry_run=effective_dry
            )
            print(
                "crypto lifecycle tape — exact-cycle anchor feed "
                "(research infrastructure only, never advice)"
            )
            print(
                f"status={r['status']}  source_crypto_run_id={r['source_crypto_run_id']}  "
                f"external_calls={r['external_calls']}"
            )
            print(
                f"tokens_received={r['tokens_received']}  "
                f"tokens_validated={r['tokens_validated']}  "
                f"anchors_attempted={r['anchors_attempted']}  "
                f"anchors_created={r['anchors_created']}  "
                f"anchors_existing={r['anchors_existing']}  "
                f"complete={r['complete_anchors']}  "
                f"incomplete={r['incomplete_anchors']}"
            )
            if r["error"]:
                print(f"rejected: {r['error']}")
            if r["status"] in ("ok", "dry_run"):
                if effective_dry and not dry_run:
                    print(
                        "not persisted — exact-run mode persists only with "
                        "--confirm (this was a preview)"
                    )
                return r["tokens_validated"]
            return -1
        from app.services.crypto_tape import LockUnavailableError

        try:
            r = CryptoLifecycleTapeRecorder().run_once(
                session, limit=limit, hours=hours, dry_run=dry_run
            )
        except LockUnavailableError as exc:
            # MEDIUM fix: an unwritable/missing lock directory must surface
            # as a typed refused status, not an uncaught traceback.
            print("crypto lifecycle tape — research infrastructure only, never advice")
            print(f"status=lock_unavailable  external_calls=0  error={exc}")
            return -1
        print("crypto lifecycle tape — research infrastructure only, never advice")
        print(f"status={r['status']}  external_calls={r['external_calls']}")
        if r.get("error"):
            print(f"error: {r['error']}")
        print(
            f"window={r['window_hours']}h  tokens_considered={r['tokens_considered']}  "
            f"birth_events={r['birth_events_created']}  "
            f"snapshots={r['snapshots_created']}  "
            f"actor_observations={r['actor_observations_created']}  "
            f"outcomes={r['outcomes_updated']}"
        )
        print(
            "provider coverage: "
            + ", ".join(f"{k}={v}" for k, v in sorted(r["provider_coverage"].items()))
        )
        if r["survival_label_mix"]:
            print(
                "survival labels (true counts): "
                + ", ".join(f"{k}={v}" for k, v in r["survival_label_mix"].items())
            )
        for e in r["examples"]:
            print(
                f"  {e['symbol'] or '?':<10} {e['token']:<16} "
                f"launch={e['launch_source']} risk={e['risk_level']} "
                f"top10={e['top10_holder_pct']} labels={e['labels']}"
            )
        if r.get("tape_run_id"):
            print(f"tape_run_id={r['tape_run_id']}")
        # A terminal non-normal status (overlap/contention-skipped, or a
        # deadline/contention-stopped partial or dry-run-partial pass) means
        # eligible work was NOT reconciled/measured — it must not exit 0 just
        # because `tokens_considered` happens to be >= 0.
        if r["status"] not in ("ok", "dry_run"):
            return -1
        return r["tokens_considered"]
    finally:
        if owns_session:
            session.close()


def _print_reconciler_guard_block(r: dict) -> None:
    """CRYPTO-RECONCILER-GUARDED-TIMER-001 — the guard rails' own output, on
    EVERY governed outcome including the skipped and refused ones.

    Three questions an unattended operator has, answered in three lines:
    did this run cross a chosen policy line, is this run marked for review,
    and is the SCHEDULE still allowed to run at all."""
    from app.services import crypto_reconciler_guard as guard

    print(
        f"batch_lock_wait_ms_max={r.get('batch_lock_wait_ms_max')}  "
        f"batch_lock_wait_warnings={r.get('batch_lock_wait_warnings')}  "
        f"batch_lock_wait_aborts={r.get('batch_lock_wait_aborts')}  "
        f"(chosen policy: review>={guard.BATCH_LOCK_WAIT_REVIEW_MS}ms, "
        f"warn>={r.get('batch_lock_wait_warn_ms')}ms, "
        f"abort>={r.get('batch_lock_wait_abort_threshold_ms')}ms — the lock-wait "
        f"BUDGET caps the wait, these lines cap the LADDER)"
    )
    print(
        f"review_required={r.get('review_required')}  "
        f"review_reasons={r.get('review_reasons')}"
    )
    print(
        f"health_gate_state={r.get('health_gate_state')}  "
        f"health_gate_tripped={r.get('health_gate_tripped')}  "
        f"health_latched={r.get('health_latched')}  "
        f"health_gate={r.get('health_gate')}"
    )
    if r.get("health_gate_tripped"):
        print(
            "!! HEALTH GATE TRIPPED — further SCHEDULED runs are blocked "
            "until a human clears the latch:"
        )
        for reason in r.get("health_gate_reasons") or []:
            print(f"   - {reason}")
        print(
            "   clear with: crypto-reconciler-health --clear --operator <name> "
            "--note '<what you checked>'   (nothing clears it automatically)"
        )
    elif r.get("health_latched"):
        print(
            "!! the health latch is TRIPPED (this invocation is an attended "
            "bypass or a recorded skip) — see crypto-reconciler-health"
        )


async def crypto_reconciler_health(
    clear: bool = False, operator: str | None = None, note: str | None = None,
    settings=None,
) -> int:
    """CRYPTO-RECONCILER-GUARDED-TIMER-001 — read (and only deliberately
    clear) the reconciler's unattended health latch.

    THE DISABLE PATH IS NOT HERE. To stop the reconciler, set
    `ENABLE_CRYPTO_TAPE_RECONCILER=false` (or stop the timer unit); this
    command manages the AUTOMATIC latch, which blocks scheduled runs after the
    rolling gate detects a trend. Clearing requires a named operator on
    purpose: the latch exists to force a human decision, and an unnamed clear
    is not one. Returns 0 when nothing is latched, -1 when a latch is (still)
    in force."""
    from app.config import get_settings
    from app.services import crypto_reconciler_guard as guard
    from app.services.crypto_tape import CryptoTapeConfig

    s = settings if settings is not None else get_settings()
    chain = CryptoTapeConfig.from_settings(s).chain
    path = guard.health_state_path(getattr(s, "database_url", None), chain)
    print(
        "crypto reconciler health — unattended guard state "
        "(research infrastructure only, never advice)"
    )
    if path is None:
        print(
            f"state=inert  chain={chain}  reason=no durable file-backed SQLite "
            "database; the rolling gate keeps no state and blocks nothing"
        )
        return 0
    state = guard.load_state(path, chain)
    window = guard.gate_window(state)
    verdict = guard.evaluate_health(window)
    latch = guard.latch_summary(state)
    print(f"state_path={path}  runs_recorded={len(state.get('runs', []))}")
    # BEFORE-ENABLE (independent review): `lock_wait_distribution_eligible`
    # justifies excluding pre-flight skips from the wait distribution by
    # promising they are "excluded and COUNTED — the skip rate is a result in
    # its own right (`crypto-reconciler-health` prints it)". It did not. A
    # reviewer built a degraded week (9 `marketops_degraded` skips in 28
    # recorded runs, a 32% skip rate) and read back `latch=CLEAR
    # review_runs=0 consecutive_marketops_skips=0` — a clean bill of health
    # while a third of the week's passes did no work, because
    # `consecutive_marketops_skips` is a TRAILING count and the last run
    # happened to be healthy. A RATE is what an operator judges; a trailing
    # count is what the gate disables on. Both are printed, and neither
    # pretends to be the other.
    all_runs = list(state.get("runs", []))
    history_skips = guard.skip_summary(all_runs)
    window_skips = guard.skip_summary(window)
    # THREE SPANS, THREE NAMES — never one word for two denominators. A
    # reviewer read two ADJACENT lines both labelled `gate_window`, one
    # reporting everything since the last clear and one reporting only the last
    # HEALTH_WINDOW_RUNS, and could plausibly have read `skip_rate=0.3214` as
    # "of the last 4 runs". Every line below names its span and carries its own
    # denominator, so no two spans share a word. (On a host that has never been
    # cleared `retained_history` and `since_clear` are the SAME runs and the two
    # lines are identical; that is expected, and is why both are labelled rather
    # than one being dropped.)
    print(
        f"retained_history skips_total={history_skips['skips_total']}  "
        f"skip_rate={history_skips['skip_rate']}  "
        f"runs_total={history_skips['runs_total']}  "
        f"skips_by_status={history_skips['skips_by_status']}  "
        f"contention_total={history_skips['contention_total']}  "
        f"contention_rate={history_skips['contention_rate']}  "
        f"(every retained run, bounded at "
        f"{guard.HEALTH_HISTORY_MAX_RECORDS} runs)"
    )
    print(
        f"since_clear skips_total={window_skips['skips_total']}  "
        f"skip_rate={window_skips['skip_rate']}  "
        f"runs_total={window_skips['runs_total']}  "
        f"skips_by_status={window_skips['skips_by_status']}  "
        f"(every run since the last --clear; the records the gate may evaluate)"
    )
    print(
        f"distribution_excluded_total="
        f"{history_skips['distribution_excluded_total']}  "
        f"distribution_excluded_by_status="
        f"{history_skips['distribution_excluded_by_status']}  "
        f"(over retained_history; pre-flight skips PLUS prelude-blocked "
        f"db_locked passes, excluded from the lock-wait distribution and "
        f"counted here)"
    )
    print(
        f"last_{guard.HEALTH_WINDOW_RUNS} runs_evaluated={verdict['window_runs']}  "
        f"severe_wait_runs={verdict['severe_wait_runs']}  "
        f"consecutive_contention_runs={verdict['consecutive_contention_runs']}  "
        f"consecutive_marketops_skips={verdict['consecutive_marketops_skips']}  "
        f"review_runs={verdict['review_runs']}  "
        f"(the rolling window the 2-in-4 / 3-in-4 rules read; the two trailing "
        f"counts run back through `since_clear`, not only these "
        f"{guard.HEALTH_WINDOW_RUNS})"
    )
    print(
        f"policy: review-mark>={guard.BATCH_LOCK_WAIT_REVIEW_MS}ms batch wait, "
        f"severe>={guard.HEALTH_SEVERE_WAIT_RUNS} in "
        f"{guard.HEALTH_WINDOW_RUNS}, contention>="
        f"{guard.HEALTH_CONSECUTIVE_CONTENTION_RUNS} consecutive, "
        f"marketops>={guard.HEALTH_CONSECUTIVE_MARKETOPS_SKIP_RUNS} "
        f"consecutive, review>={guard.HEALTH_REVIEW_RUNS} in "
        f"{guard.HEALTH_WINDOW_RUNS} — all CHOSEN operational policy, not "
        "measured constants"
    )
    for record in window[-guard.HEALTH_WINDOW_RUNS:]:
        print(
            f"  run seq={record.get('seq')} at={record.get('at')} "
            f"status={record.get('status')} stop_reason={record.get('stop_reason')} "
            f"batch_lock_wait_ms_max={record.get('batch_lock_wait_ms_max')} "
            f"adaptive_batching_active={record.get('adaptive_batching_active')} "
            f"review={record.get('review')} {record.get('review_reasons')}"
        )
    if latch is None:
        print("latch=CLEAR  scheduled runs are allowed")
        return 0
    print(f"latch=TRIPPED  tripped_at={latch.get('tripped_at')}")
    for reason in latch.get("reasons") or []:
        print(f"   - {reason}")
    if not clear:
        print(
            "scheduled runs are BLOCKED (status=skipped_health_latch). An "
            "attended `crypto-tape-reconcile --force` still runs. Clear with "
            "--clear --operator <name> --note '<what you checked>'."
        )
        return -1
    if not operator:
        print("refusing to clear: --clear requires --operator <name>")
        return -1
    cleared = guard.clear_latch(state, operator=operator, note=note)
    guard.save_state(path, state)
    print(
        f"latch CLEARED by {operator} at {cleared.get('cleared_at')}  "
        f"note={note!r}  (the trip is retained in latch_history; the rolling "
        f"window restarts at seq>{state.get('gate_window_start_seq')})"
    )
    return 0


async def crypto_tape_reconcile(
    dry_run: bool = False, force: bool = False, hours: int | None = None,
    limit: int | None = None, batch_size: int | None = None,
    max_duration_seconds: float | None = None,
    time_budget_seconds: float | None = None,
    initial_per_token_cost_seconds: float | None = None,
    lock_wait_budget_seconds: float | None = None,
    session=None,
) -> int:
    """CRYPTO-COVERAGE-REPAIR-001 scheduled provider-free reconciliation: one
    bounded pass that revisits already-persisted tokens whose survival horizons
    have matured, so 6h/24h outcomes can populate from evidence we ALREADY
    hold. Gated by enable_crypto_tape_reconciler (default OFF = clean no-op).
    Zero external calls, no discovery scan, no cohort, no arming, no execution
    semantics. Idempotent and restart-safe. Returns tokens considered."""
    from app.services.crypto_tape import run_scheduled_reconciliation

    owns_session = session is None
    if owns_session:
        # Deliberately NO run_migrations() here. A dark, default-OFF timer must
        # not apply Alembic to whatever DATABASE_URL points at, unattended,
        # every few hours and outside the deploy runbook. Same precedent as the
        # read-only report commands in this file.
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
    try:
        # HIGH-2: only forward batch_size/max_duration_seconds when the
        # caller actually set them — omitting the kwarg entirely when None is
        # semantically identical to run_scheduled_reconciliation (which
        # itself falls back to settings on None), but keeps this a true
        # pass-through for callers/tests that patch
        # run_scheduled_reconciliation and rely on kwargs.setdefault(...) for
        # keys the CLI did not explicitly set.
        extra_kwargs = {}
        if batch_size is not None:
            extra_kwargs["batch_size"] = batch_size
        if max_duration_seconds is not None:
            extra_kwargs["max_duration_seconds"] = max_duration_seconds
        # CRYPTO-COVERAGE-REPAIR-001 B1/B3/B11: same "only forward when the
        # caller actually set it" pattern as batch_size/max_duration_seconds
        # above. `initial_per_token_cost_seconds` is an UNCALIBRATED value —
        # this CLI flag exists only so a real, measured value can activate
        # adaptive batching later; there is no repo-side default to fall
        # back to.
        if time_budget_seconds is not None:
            extra_kwargs["time_budget_seconds"] = time_budget_seconds
        if initial_per_token_cost_seconds is not None:
            extra_kwargs["initial_per_token_cost_seconds"] = initial_per_token_cost_seconds
        # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: same pass-through pattern.
        # Omitted entirely when unset so the deadline-derived budget governs.
        if lock_wait_budget_seconds is not None:
            extra_kwargs["lock_wait_budget_seconds"] = lock_wait_budget_seconds
        r = run_scheduled_reconciliation(
            session, dry_run=dry_run, force=force, window_hours=hours, limit=limit,
            **extra_kwargs,
        )
        print("crypto tape reconciliation — research infrastructure only, never advice")
        if r["status"] == "disabled":
            print(
                "status=disabled  external_calls=0  no-op "
                f"(flag {r['flag']} is off)"
            )
            return 0
        # LOW fix (fourth re-review): ALLOW-list, not a deny-list, mirroring
        # crypto_lifecycle_tape's own :2869. A deny-list silently exits 0 for
        # any FUTURE status nobody remembered to add here — and the
        # fall-through print block below reads `r['window_hours']` /
        # `r['selection_limit']` / etc., which a `_refused()`-shaped result
        # (this branch's own early-return shape) does not have, so a status
        # that should have hit this branch but was missing from the deny
        # list would both misreport success AND crash on the next print
        # with a KeyError. Every status reaching the print block below MUST
        # carry the full success/partial shape (`window_hours`,
        # `selection_limit`, `tokens_considered`, ...) — only "ok",
        # "dry_run", "truncated", "partial", "dry_run_partial", and
        # "backlog_expiring" do (the non-"ok"/"dry_run" ones are also
        # allowed through here because they still carry that shape; their
        # own non-zero exit is handled separately below, after the print).
        # NEW-HIGH-2 fix (second re-review): "backlog_expiring" added — it
        # is set on the normal (non-`_refused()`) summary shape by
        # `run_scheduled_reconciliation`, same as truncated/partial.
        if r["status"] not in (
            "ok", "dry_run", "truncated", "partial", "dry_run_partial",
            "backlog_expiring", "unsafe_host_cost",
        ):
            # Non-zero: a unit that reconciles nothing must never look healthy.
            print(f"status={r['status']}  external_calls=0  error={r['error']}")
            # A3 (independent review): a refusal carries the whole lock-wait
            # contract (BLOCKER-1(a)) but this branch used to print none of
            # it — so the `db_locked` passes that carry the most information
            # for the recurring-timer gate were invisible to the operator
            # running the counted `--force` passes. Print the fields that
            # decide how the row is counted, including whether it belongs in
            # the wait distribution at all or in the separately-counted
            # prelude-blocked tally.
            print(
                f"lock_wait_ms={r.get('lock_wait_ms')}  "
                f"lock_wait_measurements={r.get('lock_wait_measurements')}  "
                f"duration_ms={r.get('duration_ms')}  "
                f"lock_wait_distribution_eligible="
                f"{r.get('lock_wait_distribution_eligible')}"
            )
            print(f"lock_wait_histogram_ms={r.get('lock_wait_histogram_ms')}")
            # CRYPTO-RECONCILER-LOCK-WAIT-PHASE-ATTRIBUTION-001 — an abandoned
            # pass carries the most contention information of any pass, so the
            # phase split has to reach the operator here too: the total tail
            # alone cannot say whether the wait was batch contention or the
            # pass's own once-per-pass bookkeeping commits.
            print(
                f"lock_wait_decision_tail_batch="
                f"{r.get('lock_wait_decision_tail_batch')}  "
                f"lock_wait_decision_tail_finalize="
                f"{r.get('lock_wait_decision_tail_finalize')}  "
                f"lock_wait_phases={r.get('lock_wait_phases')}"
            )
            # CRYPTO-RECONCILER-GUARDED-TIMER-001: a SKIP is an outcome, and
            # the operator's first question about one is always "why, and is
            # the schedule still running?". Both answers print here.
            _print_reconciler_guard_block(r)
            return -1
        print(
            f"status={r['status']}  external_calls={r['external_calls']}  "
            f"window={r['window_hours']}h  limit={r['selection_limit']}  "
            f"batch_size={r.get('batch_size')}  "
            f"gate_bypassed={r.get('gate_bypassed')}  "
            f"duration_ms={r['duration_ms']}"
        )
        print(
            f"tokens_considered={r['tokens_considered']}  "
            f"universe_size={r.get('universe_size')}  "
            f"backlog_size={r.get('backlog_size')}  "
            f"backlog_processed={r.get('backlog_processed')}  "
            f"truncated={r.get('truncated')}  "
            f"omitted={r.get('tokens_omitted')}  "
            f"stop_reason={r.get('stop_reason')}"
        )
        # NEW-HIGH-2 fix (second re-review, convergence lens): the FRONTIER
        # — the age of the oldest still-open/never-reconciled token — is the
        # metric that actually distinguishes a healthy pass from partial
        # starvation (NEW-BLOCKING-2): every other observable above can look
        # identical in both cases while this number silently marches toward
        # crypto_retention_days. Print it unconditionally so an operator
        # never has to inspect the run row's JSON `config` blob to see it.
        print(
            f"oldest_unreconciled_age_hours={r.get('oldest_unreconciled_age_hours')}  "
            f"oldest_unreconciled_first_seen_at="
            f"{r.get('oldest_unreconciled_first_seen_at')}"
        )
        # CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R7 — the RAW
        # frontier printed above is the one this branch just proved reads
        # ~204h on EVO whatever the pass does, because a single permanent
        # write-off pins it forever (B3). The RECOVERABLE frontier is the
        # one that actually moves, and until now it existed only inside the
        # `backlog_expiring` error string — invisible on every healthy pass,
        # which is exactly when an operator needs to see it trending. Print
        # it next to the raw one so the two can be compared at a glance.
        print(
            f"recoverable_backlog_count={r.get('recoverable_backlog_count')}  "
            f"oldest_recoverable_age_seconds="
            f"{r.get('oldest_recoverable_age_seconds')}  "
            f"recoverable_at_retention_risk="
            f"{r.get('recoverable_at_retention_risk')}  "
            f"writeoff_count={r.get('writeoff_count')}"
        )
        # R1 — the full-backlog classification runs BEFORE `_assemble_pass`
        # and is therefore outside everything `max_duration_seconds` can
        # bound, while still counting toward `duration_ms`. Printing it is
        # what makes a deadline overshoot diagnosable instead of mysterious.
        # A4 (independent review): `classify_ms` covers ONLY `classify_backlog`
        # while five more prelude queries run inside the same budgeted block,
        # so a block in any of them inflated `duration_ms` alone. `prelude_ms`
        # is the whole block, and `prelude_ms - classify_ms` is the part that
        # used to be unattributable.
        print(
            f"prelude_ms={r.get('prelude_ms')}  "
            f"classify_ms={r.get('classify_ms')}"
        )
        # A5 (independent review): the pass's own wall time against its
        # MODELLED worst case, per pass, because "observed non-exceedance
        # across the counted passes" is the recurring-timer precondition and
        # no constant can be one (the overshoot term tracks host load).
        # `lock_wait_distribution_eligible` says whether this row may be
        # summed into the wait distribution at all.
        print(
            f"wall_time_model_ms={r.get('wall_time_model_ms')}  "
            f"wall_time_model_exceeded={r.get('wall_time_model_exceeded')}  "
            f"lock_wait_distribution_eligible="
            f"{r.get('lock_wait_distribution_eligible')}"
        )
        # CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001 — the measured lock WAIT,
        # printed next to `blocked_ms` (which is wait PLUS the pass's own
        # write work) precisely so the two are never read as the same
        # number. `lock_wait_budget_ms` is the budget that was actually in
        # force; `write_hold_ms_max` is the largest single write-lock HOLD,
        # the quantity the 2.0s write-time SLO is about.
        print(
            f"lock_wait_ms={r.get('lock_wait_ms')}  "
            f"lock_wait_ms_max={r.get('lock_wait_ms_max')}  "
            f"lock_wait_measurements={r.get('lock_wait_measurements')}  "
            f"lock_wait_budget_ms={r.get('lock_wait_budget_ms')}  "
            f"lock_wait_budget_ms_min={r.get('lock_wait_budget_ms_min')}"
        )
        print(
            f"write_hold_ms_max={r.get('write_hold_ms_max')}  "
            f"write_hold_slo_seconds={r.get('write_hold_slo_seconds')}  "
            f"write_hold_slo_violations={r.get('write_hold_slo_violations')}  "
            f"lock_wait_histogram_ms={r.get('lock_wait_histogram_ms')}"
        )
        # CRYPTO-RECONCILER-LOCK-WAIT-PHASE-ATTRIBUTION-001 — the `>=1000 ms`
        # decision tail SPLIT BY PHASE. Three production passes each reported
        # EXACTLY ONE sample in that bucket (never zero, never two), which is
        # the signature of a once-per-pass systematic event rather than of
        # co-tenant contention; the pass has two such events (the run row's
        # creation commit and its finalize commit). `..._batch` is the only one
        # a recurring-timer threshold may be read from; the finalize's wait is
        # printed next to it because it is what `TimeoutStartSec` has to cover,
        # not because it belongs in the contention number.
        print(
            f"lock_wait_decision_tail={r.get('lock_wait_decision_tail')}  "
            f"lock_wait_decision_tail_batch="
            f"{r.get('lock_wait_decision_tail_batch')}  "
            f"lock_wait_decision_tail_finalize="
            f"{r.get('lock_wait_decision_tail_finalize')}  "
            f"finalize_lock_wait_ms={r.get('finalize_lock_wait_ms')}"
        )
        print(f"lock_wait_phases={r.get('lock_wait_phases')}")
        print(
            f"batches_committed={r.get('batches_committed')}  "
            f"lock_retry_events={r.get('lock_retry_events')}  "
            # NEW-MEDIUM-2 fix (third Lane-B review, SQLite coexistence):
            # lock_retry_events alone UNDERCOUNTS real blocked time — see
            # crypto_tape.py's comment on `blocked_seconds` for the measured
            # gap (a 40s external hold produced lock_retry_events=0).
            f"blocked_ms={r.get('blocked_ms')}  "
            f"tape_run_id={r.get('tape_run_id')}"
        )
        print(
            f"outcomes_updated={r.get('outcomes_updated')}  "
            f"snapshots={r.get('snapshots_created')}  "
            f"actor_observations={r.get('actor_observations_created')}  "
            f"snapshots_skipped_redundant={r.get('snapshots_skipped_redundant')}  "
            f"actor_observations_skipped_redundant="
            f"{r.get('actor_observations_skipped_redundant')}"
        )
        if r.get("survival_label_mix"):
            print(
                "survival labels (true counts): "
                + ", ".join(f"{k}={v}" for k, v in r["survival_label_mix"].items())
            )
        _print_reconciler_guard_block(r)
        # CRYPTO-RECONCILER-GUARDED-TIMER-001: a tripped gate must make the
        # UNIT go red even when the pass itself completed `ok`. A recurring
        # timer's loudest available channel is its own exit status, and a
        # gate that trips silently into a green unit is decoration.
        if r.get("health_gate_tripped"):
            return -1
        # LOW fix: ALLOW-list here too — only "ok"/"dry_run" are genuinely
        # healthy-complete. Everything else reaching this point (truncated /
        # partial / dry_run_partial today, plus whatever a future status
        # adds) means "eligible work remains unmeasured/unreconciled" and
        # must not exit 0.
        if r["status"] not in ("ok", "dry_run"):
            print(f"WARNING: {r['error']}")
            return -1
        return r["tokens_considered"]
    finally:
        if owns_session:
            session.close()


async def crypto_tape_session(
    duration_hours: int = 6, interval_min: int = 30, limit: int | None = None,
    dry_run: bool = False, session=None,
) -> int:
    """CRYPTO-TAPE-CADENCE-001 bounded manual tape session: a fixed, hard-capped
    number of derived run_once passes in ONE invocation, then exit — not a
    timer, not a daemon, never autonomous. Zero external calls. Aborts on
    abnormal pass status or detectable MarketOps error. Measurement only;
    never advice. Returns captures run."""
    from app.services.crypto_tape import run_tape_session

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await run_tape_session(
            session, duration_hours=duration_hours, interval_min=interval_min,
            limit=limit, dry_run=dry_run,
        )
        print("crypto tape session — measurement only, never advice")
        print(r["note"])
        print(
            f"status={r['status']}"
            + (f"  ABORT: {r['abort_reason']}" if r["abort_reason"] else "")
        )
        if r.get("aborted"):
            print(
                f"aborted=True  abort_reason={r['abort_reason']}  "
                f"failed_capture_index={r.get('failed_capture_index')}  "
                f"rows_written_before_abort={r.get('rows_written_before_abort', 0)}"
            )
            if r["abort_reason"] == "database_locked":
                print(
                    "  ! database was locked past the retry budget — check MarketOps / "
                    "tick-aggregation write contention, then rerun (run in tmux)"
                )
        print(
            f"duration_hours={r['duration_hours']}  interval_min={r['interval_min']}  "
            f"captures={r['captures_run']}/{r['captures_planned']}"
        )
        schedule = r["planned_schedule_min"]
        preview = ", ".join(f"+{m}m" for m in schedule[:8])
        print(
            f"planned schedule: {preview}"
            + (f" … (+{len(schedule) - 8} more)" if len(schedule) > 8 else "")
        )
        print(f"capture_statuses={r['capture_statuses']}")
        if r.get("probe"):
            p = r["probe"]
            print(
                f"dry probe: tokens_considered={p['tokens_considered']}  "
                f"external_calls={p['external_calls']}  "
                f"labels={p['survival_label_mix']}"
            )
        if r["provider_gap_trend"]:
            t = r["provider_gap_trend"]
            print(
                f"provider_gap share: first={t['first_capture_gap_share']} "
                f"last={t['last_capture_gap_share']} ({t['direction']})"
            )
        s = r["session_summary"]
        if s.get("available"):
            print(
                f"session: runs={s['runs']}  totals={s['totals']}  "
                f"outcomes_tracked={s['outcomes_tracked']} "
                f"(final={s['outcomes_final']})"
            )
            print("horizon maturity (known/unknown):")
            for label, mix in s["horizon_maturity"].items():
                print(f"  {label:<14} {mix['known']}/{mix['unknown']}")
            print(
                f"provider_gap_true={s['provider_gap_true']}  "
                f"db_impact_rows={s['db_impact_rows']}"
            )
        else:
            print(f"session summary: {s['reason']}")
        return r["captures_run"]
    finally:
        if owns_session:
            session.close()


async def crypto_tape_report(hours: int = 24, top: int = 5, session=None) -> int:
    """CRYPTO-TAPE-001 tape report: volumes, provider coverage, survival label
    distribution, risk distribution, actor-pattern examples, missing data.
    DB-only; read-only; never advice. Returns tape run count."""
    from app.services.crypto_tape import build_tape_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_tape_report(session, hours=hours, top=top)
        print("crypto lifecycle tape report — research infrastructure only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h  generated_at={r['generated_at']}"
            f"\ntape_runs={r['tape_runs']}  tokens_observed={r['tokens_observed']}  "
            f"birth_events={r['birth_events_in_window']}  "
            f"snapshots={r['snapshots_recorded']}  "
            f"actor_observations={r['actor_observations_recorded']}"
        )
        print(
            f"outcomes: computed={r['outcomes_computed']}  final={r['outcomes_final']}"
        )
        if r["provider_coverage_mix"]:
            print(
                "provider coverage: "
                + ", ".join(f"{k}={v}" for k, v in r["provider_coverage_mix"].items())
            )
        if r["risk_level_mix"]:
            print(
                "risk levels: "
                + ", ".join(f"{k}={v}" for k, v in r["risk_level_mix"].items())
            )
        print("survival labels (true/false/unknown):")
        for label, mix in r["survival_labels"].items():
            print(f"  {label:<22} {mix['true']}/{mix['false']}/{mix['unknown']}")
        if r["actor_pattern_examples"]:
            print("actor-pattern examples (most concentrated):")
            for e in r["actor_pattern_examples"]:
                print(
                    f"  {e['token']:<16} top10={e['top10_holder_pct']} "
                    f"sniper={e['sniper_pct']} insider={e['insider_pct']} "
                    f"bundler={e['bundler_pct']} "
                    f"creator_known={e['creator_address_known']}"
                )
        if r["missing_data_mix"]:
            print(
                "missing data: "
                + ", ".join(f"{k}={v}" for k, v in r["missing_data_mix"].items())
            )
        print(f"db_impact_rows={r['db_impact_rows']}")
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["tape_runs"]
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_cohort_create(
    limit: int = 25, hours: int = 48, dry_run: bool = False, session=None,
    require_complete: bool = False, min_liquidity: float = 0.0,
    tokens: list | None = None, confirm: bool = False,
    require_shared_horizon_windows: bool = False,
) -> int:
    """CRYPTO-HORIZON-OBS-001: freeze a fixed research cohort of recently-born
    tokens for horizon observation. Read-only selection from persisted births;
    dry-run persists nothing; no external call. `--require-complete`
    (COHORT-SELECT-001) restricts to complete-lifecycle-anchor births. `--token`
    (COHORT-SELECT-002) freezes EXACTLY the requested canonical token ids (no
    freshest-first fallback; `--confirm` persists atomically). Returns members
    selected (negative on a rejected explicit request)."""
    from app.services.crypto_horizon import CryptoHorizonService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = CryptoHorizonService().create_cohort(
            session, limit=limit, hours=hours, dry_run=dry_run,
            require_complete=require_complete, min_liquidity=min_liquidity,
            tokens=list(tokens) if tokens else None, confirm=confirm,
            require_shared_horizon_windows=require_shared_horizon_windows,
        )
        print("crypto horizon cohort — observation infrastructure, never advice")
        if r.get("mode") == "explicit_token":
            print(
                f"status={r['status']}  mode=explicit_token  external_calls={r['external_calls']}  "
                f"persisted={str(r['persisted']).lower()}"
            )
            print(
                f"requested_tokens={r['requested_tokens']}  "
                f"require_complete={str(r['require_complete']).lower()}  "
                f"require_shared_horizon_windows={str(r['require_shared_horizon_windows']).lower()}"
            )
            for rep in r["token_reports"]:
                line = f"  token={rep['token']}  valid={str(rep.get('valid')).lower()}"
                if rep.get("reason"):
                    line += f"  reason={rep['reason']}"
                if rep.get("symbol"):
                    line += f"  symbol={rep['symbol']}"
                if "all_horizons_feasible" in rep:
                    line += f"  all_horizons_feasible={str(rep['all_horizons_feasible']).lower()}"
                print(line)
                for h in rep.get("horizons") or []:
                    print(
                        f"      {h['horizon']:<4} target={h['nominal_target_utc']} "
                        f"window=[{h['window_start_utc']},{h['window_close_utc']}] "
                        f"state={h['planner_state']} feasible={str(h['feasible']).lower()}"
                    )
            sp = r.get("shared_pass")
            if sp:
                print(
                    f"shared_pass_eligible={str(r['shared_pass_eligible']).lower()}  "
                    f"15m_can_enter_due_now_simultaneously="
                    f"{str(sp['fifteen_min_windows_can_enter_due_now_simultaneously']).lower()}  "
                    f"activation_grace_fits={str(sp['activation_grace_fits_shared_window']).lower()}"
                )
                print(
                    f"earliest_safe_arm={sp['earliest_safe_arm_utc']}  "
                    f"latest_safe_arm={sp['latest_safe_arm_utc']}  "
                    f"arm_now_ok={str(sp['arm_now_within_shared_window']).lower()}"
                )
                for label, iv in sp["per_horizon"].items():
                    print(
                        f"    shared {label:<4} intersection="
                        f"[{iv['intersection_start_utc']},{iv['intersection_close_utc']}] "
                        f"nonempty={str(iv['nonempty']).lower()}"
                    )
            for rej in r.get("rejections") or []:
                print(f"  REJECT token={rej['token']} reason={rej['reason']}")
            print(
                f"resulting_members={r['resulting_members']}  members_selected={r['members_selected']}"
                + (f"  cohort_id={r['cohort_id']}" if r.get("cohort_id") else "")
            )
            return r["members_selected"] if r["status"] in {"ok", "dry_run"} else -1
        # freshest-first mode (unchanged)
        print(f"status={r['status']}  external_calls={r['external_calls']}")
        print(
            f"generated_at={r.get('generated_at')}  now_utc={r.get('now_utc')}"
        )
        print(
            f"window_hours={r['window_hours']}  cutoff_utc={r.get('window_cutoff_utc')}  "
            f"filter={r.get('filter_timestamp')}  "
            f"require_complete={str(r.get('require_complete')).lower()}"
            + (f"  min_liquidity={r.get('min_liquidity')}" if r.get("require_complete") else "")
        )
        print(
            f"members_selected={r['members_selected']}  "
            f"requested_limit={r['requested_limit']}  "
            f"max_age_minutes={r.get('max_age_minutes')}"
            + (f"  cohort_id={r['cohort_id']}" if r.get("cohort_id") else "")
        )
        for e in r.get("preview") or []:
            print(
                f"  {e['symbol'] or '?':<10} {e['token']:<16} "
                f"born={e['first_evidence_at']}  age_minutes={e['age_minutes']}"
            )
        return r["members_selected"]
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_observe_once(
    cohort_id: int, limit: int = 25, dry_run: bool = False, session=None,
) -> int:
    """CRYPTO-HORIZON-OBS-001: one manual, bounded observation pass over
    currently-due horizons for a cohort. Dry-run makes ZERO external calls and
    persists nothing. A real pass fetches via DexScreener (no SolanaTracker),
    persists ordinary ticks + audit rows, reports provider calls. Manual only —
    no loop, no timer. Returns observations recorded (0 for dry-run)."""
    from app.services.crypto_horizon import CryptoHorizonService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await CryptoHorizonService().observe_once(
            session, cohort_id=cohort_id, limit=limit, dry_run=dry_run
        )
        print("crypto horizon observe — observation only, never advice")
        print(f"status={r['status']}  external_calls={r.get('external_calls', 0)}")
        if r["status"] == "rolling_cohort_not_observable":
            print(f"  {r['error']}")
            return -1
        if r["status"] == "no_cohort":
            print(f"  no members for cohort_id={cohort_id}")
            return 0
        print(
            f"due_tokens={r.get('due_tokens')}  due_observations={r.get('due_observations')}  "
            f"cap={r.get('cap')}"
        )
        if r["status"] == "dry_run":
            print(f"would_fetch_tokens={r.get('would_fetch_tokens')}  (no calls, nothing persisted)")
            print(f"plan_status_counts={r.get('plan_status_counts')}")
            return 0
        print(
            f"provider={r.get('provider')}  observations_recorded={r.get('observations_recorded')}  "
            f"ticks_written={r.get('ticks_written')}"
        )
        print(f"outcome_counts={r.get('outcome_counts')}")
        return r.get("observations_recorded", 0)
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_schedule_report(
    cohort_id: int, top: int | None = None, session=None,
) -> dict:
    """Print the static manual observation schedule. No calls or writes."""
    from app.services.crypto_horizon_schedule import (
        build_schedule_report,
        format_los_angeles,
        format_utc,
    )

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_schedule_report(session, cohort_id)
        print("crypto horizon schedule — static manual timing, no automatic observation")
        print(
            f"cohort={r['cohort_id']} size={r['cohort_size']} "
            f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()}"
        )
        if r["status"] == "no_cohort":
            print("cohort not found")
            return r

        def stamp(label, value):
            print(
                f"  {label}: UTC={format_utc(value) or 'none'}  "
                f"America/Los_Angeles={format_los_angeles(value) or 'none'}"
            )

        print("cohort summary:")
        stamp("current time", r["now"])
        stamp("next window opening", r["next_window_opening"])
        stamp("next target time", r["next_target_time"])
        stamp("next window closing", r["next_window_closing"])
        openings = r["opening_within_minutes"]
        print(
            f"  already_observed={r['already_observed']} currently_due={r['currently_due']} "
            f"overdue={r['overdue']}"
        )
        print(
            "  opening_within: "
            f"10m={openings[10]} 30m={openings[30]} 60m={openings[60]}"
        )
        print(f"  recommended manual dry-run: {r['recommended_dry_run_command']}")
        for warning in r["warnings"]:
            print(f"WARNING: {warning}")

        rows = r["entries"]
        if top is not None:
            member_ids = []
            for row in rows:
                if row["member_id"] not in member_ids:
                    member_ids.append(row["member_id"])
            shown = set(member_ids[:max(0, top)])
            rows = [row for row in rows if row["member_id"] in shown]
            print(f"detail: showing {len(shown)} of {r['cohort_size']} cohort members")
        else:
            print("detail: all cohort members and horizons")

        for row in rows:
            token = row["symbol"] or "unknown"
            print(f"\n{token}  {row['token_address']}  horizon={row['horizon']}")
            stamp("birth anchor", row["birth_at"])
            stamp("exact target", row["target_at"])
            stamp("window start", row["window_start"])
            stamp("window end", row["window_end"])
            stamp("current time", row["now"])
            stamp("next manual action", row["recommended_next_manual_action_at"])
            print(
                f"  status={row['status']} planner_status={row['planner_status']} "
                f"observe_eligible_now={str(row['observe_eligible_now']).lower()}"
            )
            print(
                "  minutes until: "
                f"opens={row['minutes_until_window_opens']} "
                f"target={row['minutes_until_target']} "
                f"closes={row['minutes_until_window_closes']}"
            )
            print(
                "  shared bounded pass: "
                f"{str(row['can_share_bounded_pass']).lower()} "
                f"(tokens={row['shared_pass_tokens']} "
                f"observations={row['shared_pass_observations']})"
            )
        return r
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_reminder_plan(cohort_id: int, session=None) -> dict:
    """Print a static reminder plan. Nothing is installed or invoked."""
    from app.services.crypto_horizon_schedule import (
        build_reminder_plan,
        format_los_angeles,
        format_utc,
    )

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_reminder_plan(session, cohort_id)
        print("crypto horizon reminder plan — static only, not installed")
        print(
            f"cohort={r['cohort_id']} size={r['cohort_size']} "
            f"reminders={len(r['reminders'])} installed={str(r['installed']).lower()} "
            f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()}"
        )
        if r["status"] == "no_cohort":
            print("cohort not found")
            return r
        for warning in r["warnings"]:
            print(f"WARNING: {warning}")
        for reminder in r["reminders"]:
            print(f"\nreminder {reminder['id']}:")
            print(
                f"  suggested reminder: UTC={format_utc(reminder['suggested_reminder_at'])}  "
                "America/Los_Angeles="
                f"{format_los_angeles(reminder['suggested_reminder_at'])}"
            )
            print(
                f"  shared action time: UTC={format_utc(reminder['suggested_action_at'])}  "
                "America/Los_Angeles="
                f"{format_los_angeles(reminder['suggested_action_at'])}"
            )
            affected = ", ".join(
                f"{item['symbol'] or item['token_address'][:12]}:{item['horizon']}"
                for item in reminder["affected"]
            )
            print(f"  affected tokens/horizons: {affected}")
            print(f"  suggested dry-run command: {reminder['suggested_dry_run_command']}")
            print("  REAL COMMAND — REQUIRES EXPLICIT HUMAN INVOCATION:")
            print(f"    {reminder['suggested_real_command']}")
        return r
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_arm_cohort(
    cohort_id: int,
    dry_run: bool = False,
    confirm: bool = False,
    session=None,
    orchestrator=None,
) -> dict:
    """Plan or explicitly install bounded user-systemd one-shot observations."""
    from app.services.crypto_horizon_orchestrator import CryptoHorizonOrchestrator

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        orchestrator = orchestrator or CryptoHorizonOrchestrator()
        result = orchestrator.arm(
            session, cohort_id, dry_run=dry_run, confirm=confirm
        )
        print("crypto horizon cohort arming — bounded one-shot observation jobs")
        print(
            f"status={result['status']} cohort={result['cohort_id']} "
            f"size={result['cohort_size']} expected_jobs={result['expected_jobs']} "
            f"external_calls={result['external_calls']} "
            f"persisted={str(result['persisted']).lower()} "
            f"installed={str(result['installed']).lower()}"
        )
        for warning in result["warnings"]:
            print(f"WARNING: {warning}")
        for job in result["jobs"]:
            print(f"\njob {job['job_id']}  unit={job['unit_base']}")
            print(
                f"  execute: UTC={job['execute_at_utc']}  "
                f"America/Los_Angeles={job['execute_at_los_angeles']}"
            )
            print(
                f"  affected_tokens={job['affected_tokens']} "
                f"affected_observations={job['affected_observations']} "
                f"cohort_limit={job['limit']}"
            )
            print(f"  command: {job['command']}")
            print(f"  log: {job['log_path']}")
        if result["status"] == "confirmation_required":
            print("No units installed. Re-run with --confirm after reviewing every job.")
        elif result["status"] == "no_future_windows":
            print("Cohort rejected: no future unobserved windows remain.")
        print(f"\ndisclaimer: {result['disclaimer']}")
        return result
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_run_job(
    cohort_id: int, job_id: int, session=None, orchestrator=None,
) -> int:
    """Internal one-attempt worker invoked only by generated one-shot units."""
    from app.services.crypto_horizon_orchestrator import CryptoHorizonOrchestrator

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        orchestrator = orchestrator or CryptoHorizonOrchestrator()
        result = await orchestrator.run_job(session, cohort_id, job_id)
        print("crypto horizon one-shot job — observation only")
        print(
            f"cohort={cohort_id} job={job_id} status={result['status']} "
            f"reason={result['reason']} exit_code={result['last_exit_code']}"
        )
        if result.get("observation_summary"):
            summary = result["observation_summary"]
            print(
                f"external_calls={summary.get('external_calls', 0)} "
                f"observations_recorded={summary.get('observations_recorded', 0)} "
                f"ticks_written={summary.get('ticks_written', 0)}"
            )
        for path in result.get("report_paths", []):
            print(f"report: {path}")
        return result["last_exit_code"]
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_orchestrator_report(
    cohort_id: int, session=None, orchestrator=None,
) -> dict:
    """Read-only status view over planned jobs, unit files, and observations."""
    from app.services.crypto_horizon_orchestrator import CryptoHorizonOrchestrator

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        orchestrator = orchestrator or CryptoHorizonOrchestrator()
        result = orchestrator.report(session, cohort_id)
        print("crypto horizon orchestrator report — host-native one-shot jobs")
        print(
            f"status={result['status']} cohort={cohort_id} "
            f"planned_jobs={result['planned_jobs']} installed_jobs={result['installed_jobs']}"
        )
        print(f"job_status={result['status_counts']}")
        print(f"next_execution_time={result['next_execution_time']}")
        print(f"observation_counts_by_horizon={result['observation_counts_by_horizon']}")
        print(f"last_exit_code={result['last_exit_code']}")
        print(
            f"any_timer_installed={str(result['any_timer_installed']).lower()} "
            "timer_remains_installed_after_completion="
            f"{str(result['timer_remains_installed_after_completion']).lower()}"
        )
        health = result["marketops_health"]
        print(
            f"MarketOps health: healthy={str(health['healthy']).lower()} "
            f"reason={health['reason']} run_id={health.get('run_id')}"
        )
        for job in result["jobs"]:
            print(
                f"  job={job['job_id']} status={job['status']} "
                f"execute_at={job['execute_at_utc']} exit={job['last_exit_code']} "
                f"timer_installed={str(job['timer_installed']).lower()} "
                f"log={job['log_path']}"
            )
        print(f"\ndisclaimer: {result['disclaimer']}")
        return result
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_disarm_cohort(
    cohort_id: int, confirm: bool = False, orchestrator=None,
) -> dict:
    """Preview or remove only the validated one-shot units for one cohort."""
    from app.services.crypto_horizon_orchestrator import CryptoHorizonOrchestrator

    orchestrator = orchestrator or CryptoHorizonOrchestrator()
    result = orchestrator.disarm(cohort_id, confirm=confirm)
    print("crypto horizon cohort disarm — isolated one-shot cleanup")
    print(f"status={result['status']} cohort={cohort_id}")
    print("units selected:")
    for unit in result["units"]:
        print(f"  {unit}")
    if not result["units"]:
        print("  none")
    if result["removed"]:
        print("removed:")
        for unit in result["removed"]:
            print(f"  {unit}")
    elif result["status"] == "confirmation_required":
        print("Nothing removed. Re-run with --confirm after reviewing the units.")
    return result


async def crypto_horizon_shared_candidate_feasibility_report(
    hours: int | None = None, days: int | None = None, limit: int = 10,
    fmt: str = "text", arm_margin_seconds: float = 0.0, session=None,
) -> dict:
    """CRYPTO-HORIZON-SHARED-CANDIDATE-FEASIBILITY-001 — from persisted local data
    only, measure whether the pipeline can yield two complete tokens with
    overlapping 15m windows early enough to arm CANARY-004. Zero calls, zero
    writes; never advice."""
    import json as _json
    from datetime import timedelta

    from app.services.crypto_horizon_feasibility import (
        DEFAULT_RANGES,
        build_feasibility_report,
    )

    ranges = list(DEFAULT_RANGES)
    if hours is not None:
        ranges.append((f"{hours}h", timedelta(hours=hours)))
    if days is not None:
        ranges.append((f"{days}d", timedelta(days=days)))
    # de-duplicate by label, preserve order
    seen, deduped = set(), []
    for label, delta in ranges:
        if label not in seen:
            seen.add(label)
            deduped.append((label, delta))

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_feasibility_report(
            session, ranges=tuple(deduped), limit=max(1, limit),
            arm_margin_seconds=max(0.0, arm_margin_seconds),
        )
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return r

    cov = r["history_coverage"]
    print("crypto horizon shared-candidate feasibility — measurement only, never advice")
    print(f"external_calls={r['external_calls']} writes={r['writes']} "
          f"persisted={str(r['persisted']).lower()} "
          f"grace={r['activation_grace_seconds']}s arm_margin={r['operator_arm_margin_seconds']}s")
    print("history coverage:")
    print(f"  anchors={cov['total_anchors']} span_days={cov['anchor_span_days']}")
    print(f"  first_evidence_at: {cov['first_evidence_min']} .. {cov['first_evidence_max']}")
    print(f"  persisted (created_at): {cov['persist_min']} .. {cov['persist_max']}")
    for rr in cov["requested_ranges"]:
        flag = "" if rr["fully_covered_by_history"] else "  [DATA-LIMITED: exceeds history]"
        print(f"  range {rr['range']}: cutoff={rr['cutoff_utc']}{flag}")
    print("completeness + arming funnel (denominator = anchors in range):")
    for label, f in r["funnels"].items():
        print(f"  [{label}] denom={f['denominator']}")
        for s in f["steps"]:
            print(f"    {s['step']:34} {s['count']:5}  {s['pct_of_all']:5.1f}%")
        print(f"    failure reasons: {f['completeness_failure_reasons']}")
    sw = r["shared_window"]
    print(f"shared 15m window feasibility (<= {sw['neighborhood_minutes']}min apart):")
    print(f"  complete pairs in neighborhood:        {sw['complete_pairs_in_neighborhood']}")
    print(f"  overlapping 15m windows:               {sw['overlapping_15m_windows']}")
    print(f"  grace-compatible shared windows:       {sw['grace_compatible_shared_windows']}")
    print(f"  USABLE (persisted before arm deadline):{sw['usable_pairs_persisted_in_time']}")
    print(f"  distinct usable moments:               {sw['distinct_usable_moments']}")
    print(f"  days with >=1 usable pair:             {sw['days_with_usable_pair']}")
    for ex in sw["examples"]:
        print(f"    usable: {ex['token_a']}+{ex['token_b']} shared=[{ex['shared_15m_open']},"
              f"{ex['shared_15m_close']}] slack={ex['arm_slack_seconds']}s")
    print("by launch_source:")
    for s in r["by_launch_source"]:
        print(f"  {str(s['name']):22} n={s['n']} complete={s['complete']} "
              f"({s['complete_rate']}) null_liq={s['null_liquidity']} "
              f"median_lag_complete={s['median_lag_seconds_complete']}s armable={s['armable_complete']}")
    print("by pair_venue (first_dex_id):")
    for s in r["by_pair_venue"]:
        print(f"  {str(s['name']):14} n={s['n']} complete={s['complete']} ({s['complete_rate']})")
    return r


async def crypto_horizon_candidate_readiness_report(
    at: str | None = None, limit: int = 5, fmt: str = "text",
    require_complete: bool = True, minimum_arm_margin_seconds: float | None = None,
    session=None,
) -> dict:
    """CRYPTO-HORIZON-CANDIDATE-READINESS-001 — is there, right now, an exact
    two-token shared-pass candidate set in local persisted data? Zero calls, zero
    writes; manual review required, nothing armed. Historical feasibility is NOT
    current readiness — this reports the current state only."""
    import json as _json

    from app.services.crypto_horizon_readiness import (
        OPERATOR_PREP_MARGIN_SECONDS,
        evaluate_readiness,
        proposed_commands,
    )

    now = None
    if at is not None:
        from datetime import datetime as _dt

        now = _dt.fromisoformat(at.replace("Z", "+00:00"))
    margin = OPERATOR_PREP_MARGIN_SECONDS if minimum_arm_margin_seconds is None \
        else max(0.0, minimum_arm_margin_seconds)

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = evaluate_readiness(
            session, now=now, min_arm_margin_seconds=margin,
            require_complete=require_complete, limit=max(1, limit),
        )
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return r

    c = r["counts"]
    print("crypto horizon candidate readiness — operational canary readiness, manual review required")
    print(f"status={r['state']}")
    print(f"external_calls={r['external_calls']} persisted={str(r['persisted']).lower()} "
          f"writes={r['writes']} manual_action_required={str(r['manual_action_required']).lower()} "
          f"automatic_cohort_creation={str(r['automatic_cohort_creation']).lower()} "
          f"automatic_arming={str(r['automatic_arming']).lower()}")
    print(f"evaluated_at UTC={r['evaluated_at_utc']}  LA={r['evaluated_at_los_angeles']}")
    print(f"db_coverage UTC={r['database_coverage_utc']} persist_max={r['database_persist_max_utc']} "
          f"latest_discovery_run_id={r['latest_discovery_run_id']}")
    print(f"grace={r['activation_grace_seconds']}s min_arm_margin={r['minimum_arm_margin_seconds']}s "
          f"require_complete={str(r['require_complete']).lower()}")
    print(f"counts: candidates={c['candidates']} complete={c['complete_candidates']} "
          f"feasible_15m={c['feasible_15m_candidates']} all_horizons_feasible="
          f"{c['all_horizons_feasible_candidates']} overlapping_pairs={c['overlapping_pairs']} "
          f"usable_pairs={c['usable_pairs']} rejected={c['rejected']}")
    print(f"pair ordering: {r['pair_ordering']}")
    top = r["top_pair"]
    if top is None:
        print("no candidate pair to detail at this evaluation time (historical feasibility != current readiness)")
    else:
        print("highest-priority pair:")
        print(f"  candidate_pair={top['token_a']},{top['token_b']}  "
              f"symbols={top['symbol_a']},{top['symbol_b']}  state={top['state']}")
        print(f"  shared_window_open={top['shared_window_open_utc']} "
              f"close={top['shared_window_close_utc']}")
        print(f"  grace_adjusted_earliest_exec={top['activation_grace_adjusted_earliest_exec_utc']}")
        print(f"  earliest_safe_preparation={top['earliest_safe_preparation_utc']} "
              f"latest_safe_cohort_creation={top['latest_safe_cohort_creation_utc']} "
              f"latest_safe_arming={top['latest_safe_arming_utc']}")
        print(f"  remaining_safe_slack_seconds={top['remaining_safe_slack_seconds']} "
              f"shared_pass_eligible={str(top['shared_pass_eligible']).lower()} "
              f"not_ready_reason={top['not_ready_reason']}")
        for side in ("token_a_detail", "token_b_detail"):
            d = top[side]
            print(f"  {d['token']} ({d['symbol']}) fe={d['first_evidence_at']} "
                  f"persisted={d['persisted_at']} src={d['launch_source']} venue={d['pair_venue']} "
                  f"liq={d['initial_liquidity_usd']} price={d['initial_price_usd']} "
                  f"completeness={d['completeness']} planner_state={d['planner_state']} "
                  f"15m_window=[{d['window_15m_start']},{d['window_15m_close']}]")
    if r["rejected"]:
        print(f"rejected (fail-closed, sample): {r['rejected']}")
    cmds = proposed_commands(r)
    if cmds:
        print("proposed manual commands (operator review only — NOT executed):")
        for line in cmds:
            print(f"  {line}")
    return r


async def crypto_horizon_candidate_readiness_history_report(
    limit: int = 20, fmt: str = "text",
) -> dict:
    """Bounded aggregate over accumulated readiness evaluations (append-only audit).
    Read-only; zero calls, zero writes. Determines whether the current discovery
    cadence catches usable moments. No market-performance output."""
    import json as _json

    from app.services.crypto_horizon_readiness import (
        build_readiness_history_report,
        load_readiness_records,
    )

    records = load_readiness_records()
    r = build_readiness_history_report(records, limit=max(1, limit))
    if fmt == "json":
        print(_json.dumps(r, indent=2, default=str))
        return r
    print("crypto horizon candidate readiness history — measurement only, never advice")
    print(f"external_calls={r['external_calls']} evaluations={r['evaluations']}")
    print(f"coverage UTC: {r['history_coverage_utc']['first']} .. {r['history_coverage_utc']['last']}")
    print(f"median_cycle_gap_seconds={r['median_cycle_gap_seconds']}")
    print(f"count_by_state={r['count_by_state']}")
    print(f"distinct_ready_moments={r['distinct_ready_moments']} "
          f"distinct_candidate_pairs={r['distinct_candidate_pairs']}")
    print(f"ready_moments_by_day={r['ready_moments_by_day']}")
    print(f"max_arm_slack_seconds={r['max_arm_slack_seconds']} "
          f"median_arm_slack_seconds={r['median_arm_slack_seconds']}")
    print(f"estimated_single_cycle_moments={r['estimated_single_cycle_moments']}")
    print(f"  {r['missed_between_cycle_estimate_note']}")
    for m in r["moments"]:
        print(f"  moment pair={m['pair']} [{m['start_utc']}..{m['end_utc']}] "
              f"duration={m['duration_seconds']}s cycles={m['cycles']} max_slack={m['max_slack_seconds']}s")
    return r


async def crypto_horizon_observation_report(
    cohort_id: int, top: int = 5, shadow: bool = False, session=None,
) -> int:
    """CRYPTO-HORIZON-OBS-001 coverage report: completion/liquidity rates by
    horizon, inactive/no-pair rates, target-distance distribution, success
    gates (measurement only), examples. With --shadow, print the pre-observation
    coverage-gain + provider-load estimate instead. Read-only; never advice.
    Returns cohort size."""
    from app.services.crypto_horizon import build_observation_report, shadow_estimate

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        if shadow:
            r = shadow_estimate(session, cohort_id)
            print("crypto horizon shadow estimate — measurement only, never advice")
            print(f"cohort_id={r['cohort_id']}  external_calls={r['external_calls']}")
            print("expected coverage gain by horizon (due_now/total):")
            for label, g in r["expected_coverage_gain_by_horizon"].items():
                print(
                    f"  {label:<4} due_now={g['due_now']}/{g['total']}  "
                    f"gain={g['expected_coverage_gain']}"
                )
            print(f"required calls/day estimate (cohort 25/50/100): {r['required_calls_per_day_estimate']}")
            print(f"solana_tracker_usage: {r['solana_tracker_usage']}")
            print(f"provider_budget_supported={r['provider_budget_supported']}")
            return 0
        r = build_observation_report(session, cohort_id, top=top)
        print("crypto horizon observation report — measurement only, never advice")
        print(r["note"])
        print(f"\ncohort_id={r['cohort_id']}  cohort_size={r['cohort_size']}  "
              f"observations_total={r['observations_total']}")
        print("by horizon (explicit denominators):")
        for label in ("15m", "1h", "6h", "24h"):
            h = r["by_horizon"][label]
            print(
                f"  {label:<4} due_total={h['horizon_due_total']} due_now={h['due_now']} "
                f"overdue={h['overdue_unobserved']} attempted={h['attempted']} "
                f"observed={h['observed']} missed_attempted={h['missed_attempted']} "
                f"not_due={h['skipped_not_due']}"
            )
            print(
                f"       completion(of attempts)={h['completion_rate_of_attempts']}  "
                f"coverage(of due)={h['coverage_rate_of_due']}  "
                f"liq_field={h['liquidity_field_completion_rate']}"
            )
        print(
            f"inactive_token_rate={r['inactive_token_rate']}  "
            f"provider_no_pair_rate={r['provider_no_pair_rate']}"
        )
        if r["early_liquidity_diagnostics"]:
            print(f"early-liquidity diagnostics (15m/1h): {r['early_liquidity_diagnostics']}")
        td = r["target_distance_seconds"]
        print(f"target-distance s: p50={td['p50']} p90={td['p90']} min={td['min']} max={td['max']}")
        print("success gates (MEASUREMENT only):")
        for label, g in r["success_gates"].items():
            print(f"  {label:<14} target={g['target']} actual={g['actual']} pass={g['pass']}")
        for name, rows in r["examples"].items():
            if rows:
                print(f"{name}:")
                for e in rows:
                    print(f"  {e}")
        print(f"provider_usage={r['provider_usage']}  db_impact_rows={r['db_impact_rows']}")
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["cohort_size"]
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_pair_selection_report(
    cohort_id: int, top: int = 5, session=None
) -> int:
    """CRYPTO-HORIZON-OBS-002: for each failed (no-liquidity) observation, show
    the captured candidate pairs, whether another pair had usable liquidity, and
    which shadow policy would have selected it. Read-only; no external call.
    Returns failed-observation count."""
    from app.services.crypto_horizon import build_pair_selection_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_pair_selection_report(session, cohort_id, top=top)
        print("crypto horizon pair-selection report — diagnostic only, never advice")
        print(
            f"cohort_id={r['cohort_id']}  failed_no_liquidity={r['failed_no_liquidity']}  "
            f"avoidable={r['avoidable_failures']}  "
            f"projected_completion_improvement={r['projected_completion_improvement']}"
        )
        if r["rows_without_captured_candidates"]:
            print(
                f"  ! {r['rows_without_captured_candidates']} failed row(s) have no "
                "captured candidates (observed before OBS-002) — re-run observe to diagnose"
            )
        for e in r["examples"]:
            print(
                f"  {e['token']:<16} h={e['horizon']} pairs={e['pair_count']} "
                f"eligible={e['eligible_pair_count']} avoidable={e['no_liquidity_state_avoidable']}"
            )
            print(f"     liq_field_states={e['liquidity_field_states']}")
            print(f"     shadow_selection={e['shadow_policy_selection']}")
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["failed_no_liquidity"]
    finally:
        if owns_session:
            session.close()


async def crypto_horizon_outcome_reconciliation_report(
    cohort_id: int, top: int = 5, session=None
) -> int:
    """CRYPTO-HORIZON-OBS-002: cohort-specific proof that a horizon observation
    flips a lifecycle outcome unknown->known. Recomputes survival WITH vs
    WITHOUT each observation's exact tick (read-only), isolating its
    contribution. Nothing persisted. Returns observed-with-tick count."""
    from app.services.crypto_horizon import build_outcome_reconciliation_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_outcome_reconciliation_report(session, cohort_id, top=top)
        print("crypto horizon outcome reconciliation — measurement only, never advice")
        print(r["method"])
        print(
            f"cohort_id={r['cohort_id']}  observed_with_tick={r['observed_with_tick']}  "
            f"transitioned_unknown_to_known={r['transitioned_unknown_to_known']}  "
            f"transition_rate={r['transition_rate']}"
        )
        for e in r["reconciliation"]:
            print(
                f"  {e['token']:<16} h={e['horizon']} obs_id={e['observation_id']} "
                f"tick_id={e['tick_id']} before={e['outcome_before']} "
                f"after={e['outcome_after']} transitioned={e['transitioned_unknown_to_known']}"
                + (f" fail={e['failure_cause_if_still_unknown']}"
                   if e['failure_cause_if_still_unknown'] else "")
            )
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["observed_with_tick"]
    finally:
        if owns_session:
            session.close()


async def crypto_sparse_observe(
    dry_run: bool = False, force: bool = False,
    enrol_limit: int | None = None, observe_limit: int | None = None,
    write_batch_size: int | None = None,
    max_duration_seconds: float | None = None,
    session=None,
) -> int:
    """CRYPTO-COVERAGE-REPAIR-002 prospective sparse observation: one bounded,
    governed pass that enrols eligible NEW births into the standing rolling
    cohort and buys exactly one 6h and one 24h market/liquidity observation per
    birth via the existing DexScreener adapter. Gated by
    enable_crypto_sparse_observation (default OFF = clean no-op). No
    SolanaTracker (structurally denied), no cohort arming, no retries, no
    interpolation, no backfill. Idempotent and restart-safe. Returns
    observations recorded, or -1 on any non-healthy status."""
    from app.services.crypto_sparse_observation import (
        HEALTHY_STATUSES,
        STATUS_DISABLED,
        SparseObservationConfig,
        run_scheduled_sparse_observation,
    )

    owns_session = session is None
    if owns_session:
        # Deliberately NO migration application here, same precedent as
        # crypto-tape-reconcile: a dark, default-OFF timer must never apply
        # Alembic unattended, outside the deploy runbook.
        from app.db import get_sessionmaker

        session = get_sessionmaker()()
    try:
        base = SparseObservationConfig.from_settings()
        cfg = SparseObservationConfig(
            chain=base.chain,
            enrol_limit=base.enrol_limit if enrol_limit is None else enrol_limit,
            observe_limit=(
                base.observe_limit if observe_limit is None else observe_limit
            ),
            write_batch_size=(
                base.write_batch_size if write_batch_size is None else write_batch_size
            ),
            max_duration_seconds=(
                base.max_duration_seconds if max_duration_seconds is None
                else max_duration_seconds
            ),
        )
        r = await run_scheduled_sparse_observation(
            session, dry_run=dry_run, force=force, config=cfg,
        )
        print(
            "crypto sparse observation — prospective OBSERVATION only, "
            "never advice"
        )
        if r["status"] == STATUS_DISABLED:
            print(
                "status=disabled  external_calls=0  no-op "
                f"(flag {r['flag']} is off)"
            )
            return 0
        if r["status"] not in HEALTHY_STATUSES:
            print(
                f"status={r['status']}  external_calls={r['external_calls']}  "
                f"error={r.get('error')}"
            )
            return -1
        print(
            f"status={r['status']}  external_calls={r['external_calls']}  "
            f"provider={r['provider']}  solana_tracker_calls="
            f"{r['solana_tracker_calls']}  "
            f"gate_bypassed={r.get('gate_bypassed')}  "
            f"duration_ms={r['duration_ms']}"
        )
        print(
            f"cohort_id={r.get('cohort_id')}  "
            f"cohort_created={r.get('cohort_created')}  "
            f"births_considered={r['births_considered']}  "
            f"enrolment_rejections={r['enrolment_rejections']}"
        )
        # B5: this command deliberately does NOT call `ensure_schema_current`
        # (see above), so it is the one command that runs happily against an
        # un-migrated DB — on `_sparse_plan`'s un-indexed path, measured 416 ms
        # cold against 25 ms. Say so on the receipt rather than let a
        # load-bearing absence stay silent.
        print(f"working_set_index_present={r.get('working_set_index_present')}")
        if r.get("working_set_index_present") is False:
            print(
                "  WARNING: migration 0029 has NOT been applied to this "
                "database. The planner is on the slow, un-indexed path. Apply "
                "`alembic upgrade head` before enabling the flag."
            )
        if r["status"] == "dry_run":
            print(
                f"would_create_cohort={r.get('would_create_cohort')}  "
                f"would_enrol={r.get('would_enrol')}  "
                f"due_observations={r['due_observations']}  "
                f"would_fetch_tokens={r.get('would_fetch_tokens')}"
            )
            print(f"plan_status_counts={r.get('plan_status_counts')}")
            print("nothing was enrolled, fetched, or written")
            return 0
        print(
            f"enrolled={r['enrolled']}  due_observations={r['due_observations']}  "
            f"observations_recorded={r['observations_recorded']}  "
            f"ticks_written={r['ticks_written']}"
        )
        print(
            f"outcome_counts={r['outcome_counts']}  "
            f"batches_committed={r['batches_committed']}  "
            f"deferred={r['deferred_observations']}  "
            f"stop_reason={r['stop_reason']}"
        )
        print(
            f"retryable_request_failures={r['retryable_request_failures']}  "
            f"request_failures_reattempted={r['request_failures_reattempted']}"
        )
        print(f"write_lock={r.get('write_lock')}")
        print(f"provider_ledger={r.get('provider_ledger')}")
        return r["observations_recorded"]
    finally:
        if owns_session:
            session.close()


async def crypto_observation_coverage_report(
    hours: int | None = None, top: int = 5, session=None
) -> int:
    """CRYPTO-COVERAGE-REPAIR-002 OBSERVATION-coverage report: of the
    member-horizons whose observation band has CLOSED, how many did the sparse
    lane actually look at? This is NOT reconciliation coverage (whether a
    survival label could be computed) — that lives in
    crypto-tape-coverage-report against a different denominator. Reads no
    survival label, persists nothing, makes no external call. Returns enrolled
    members analyzed."""
    from app.services.crypto_sparse_observation import (
        build_observation_coverage_report,
    )

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_observation_coverage_report(session, hours=hours, top=top)
        print(
            "crypto OBSERVATION coverage — did a scheduled look happen; "
            "never advice"
        )
        print(f"MEASURES:     {r['this_report_measures']}")
        print(f"DOES NOT:     {r['this_report_does_not_measure']}")
        print(
            f"\nstatus={r['status']}  chain={r['chain']}  "
            f"generated_at={r['generated_at']}"
        )
        if r["status"] != "ok":
            print(f"  {r.get('error') or r.get('cohort_ids') or 'no standing rolling cohort'}")
            return 0 if r["status"] == "no_cohort" else -1
        print(
            f"cohort_id={r['cohort_id']}  enrolled_members={r['enrolled_members']}  "
            f"window_hours={r['window_hours']}  band=+/-{r['band_minutes']}min  "
            f"cadence={r['cadence_minutes']}min"
        )
        for label in r["horizons"]:
            h = r["by_horizon"][label]
            print(
                f"\n  {label}: bands_closed={h['bands_closed']} "
                f"(denominator={h['attempt_denominator']})"
            )
            print(
                f"    observed={h['observed']}  attempted_missed="
                f"{h['attempted_missed']}  out_of_band={h['out_of_band']}  "
                f"scheduling_miss={h['scheduling_miss']}  "
                f"band_open={h['band_open']}  band_not_open_yet="
                f"{h['band_not_open_yet']}"
            )
            print(
                f"    enrolled_after_band_closed="
                f"{h['enrolled_after_band_closed']}  never_had_a_chance_rate="
                f"{h['never_had_a_chance_rate']} "
                f"(denominator={h['never_had_a_chance_denominator']})"
            )
            print(
                f"    observation_attempt_rate={h['observation_attempt_rate']}  "
                f"observation_success_rate={h['observation_success_rate']} "
                f"(denominator={h['success_denominator']})"
            )
            print(
                f"    look_completion_rate={h['look_completion_rate']}  "
                f"scheduling_miss_rate={h['scheduling_miss_rate']}  "
                f"out_of_band_rate={h['out_of_band_rate']}"
            )
            if h["miss_causes"]:
                print(f"    miss_causes={h['miss_causes']}")
        live = r["liveness"]
        print(
            f"\nliveness: latest_write_at={live['latest_write_at']}  "
            f"previous_write_age_minutes={live['previous_write_age_minutes']}  "
            f"cadence={live['cadence_minutes']}min  "
            f"WARNING={live['cadence_warning']}"
        )
        if live["cadence_warning"]:
            print(f"  cadence_warning means: {live['cadence_warning_means']}")
        print(
            f"  expected timer line: OnCalendar="
            f"{live['expected_timer_oncalendar']}"
        )
        print(f"enrolment_lag_seconds={r['enrolment_lag_seconds']}")
        print(f"target_distance_seconds={r['target_distance_seconds']}")
        if r["scheduling_miss_examples"]:
            print("scheduling misses (never backfilled):")
            for e in r["scheduling_miss_examples"]:
                print(
                    f"  {e['token']} {e['horizon']} band_closed_at="
                    f"{e['band_closed_at']}"
                )
        print(f"\n{r['disclaimer']}")
        return r["enrolled_members"]
    finally:
        if owns_session:
            session.close()


async def crypto_tape_coverage_report(
    hours: int = 168, top: int = 5, limit: int = 25, session=None
) -> int:
    """CRYPTO-COVERAGE-001 tape-coverage forensics: decompose every unmeasurable
    survival horizon into an explicit cause and estimate (shadow-only) whether
    selection/revisit policy can mature 6h/24h outcomes. Compute-on-demand;
    persists nothing; no external call; changes no stored label or live
    selection; never advice. Returns tokens analyzed."""
    from app.services.crypto_coverage import build_coverage_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_coverage_report(session, hours=hours, top=top, limit=limit)
        print("crypto tape coverage forensics — diagnostic only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h  generated_at={r['generated_at']}"
            f"\ntokens_analyzed={r['tokens_analyzed']}  "
            f"selection_limit={r['selection_limit']}"
        )
        print("\ncoverage funnel (of tokens whose horizon is DUE):")
        for label in ("15m", "1h", "6h", "24h"):
            f = r["coverage_funnel"][label]
            print(
                f"  {label:<4} born={f['tokens_born']} due={f['horizon_due']} "
                f"revisited={f['revisited_after_due']} data={f['raw_market_data_available']} "
                f"in_tol={f['tick_within_tolerance']} measurable={f['outcome_measurable']} "
                f"gap={f['provider_gap']}  "
                f"(measurable_rate={f['rates_vs_due']['outcome_measurable']})"
            )
        print("\ngap causes by horizon:")
        for label in ("15m", "1h", "6h", "24h"):
            hist = r["gap_causes"].get(label) or {}
            if hist:
                print(
                    f"  {label:<4} "
                    + ", ".join(f"{c}={n}" for c, n in sorted(hist.items(), key=lambda kv: -kv[1]))
                )
        print("\nbottleneck verdict (6h/24h):")
        for label, v in r["bottleneck_verdict"].items():
            print(
                f"  {label:<4} bottleneck={v['bottleneck']}  "
                f"upstream_coverage={v['upstream_coverage_share']}  "
                f"revisit_policy={v['revisit_policy_share']}"
            )
        s = r["selection_analysis"]
        print(
            f"\nselection: appearances min/mean/max="
            f"{s['appearances_min']}/{s['appearances_mean']}/{s['appearances_max']}  "
            f"recent_first_starves_old_cohorts={s['recent_first_starves_old_cohorts']}"
        )
        print(
            f"  due tokens omitted from limit={s['due_tokens_omitted_from_limit']}  "
            f"omission_rate={s['due_token_omission_rate']}"
        )
        sh = r["shadow_selection"]
        print(
            f"\nshadow selection (est. NEW 6h/24h matures on next run of {sh['limit']}; "
            f"total available={sh['total_maturable_available']}):"
        )
        for policy, g in sh["policies"].items():
            print(
                f"  {policy:<24} total={g['expected_new_matures_total']}  "
                f"by_horizon={g['expected_new_matures_by_horizon']}"
            )
        for name, rows in r["examples"].items():
            if rows:
                print(f"\n{name}:")
                for e in rows:
                    print(
                        f"  {e['symbol'] or '?':<10} {e['token']:<16} "
                        f"h={e['horizon']} cause={e['cause']} rank={e['rank']} "
                        f"appearances={e['appearances']}"
                    )
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["tokens_analyzed"]
    finally:
        if owns_session:
            session.close()


async def crypto_retrospect_report(
    hours: int = 48, top: int = 5, cohort: str = "all", session=None
) -> int:
    """CRYPTO-RETROSPECT-001/002 retrospective feature/outcome analysis: which
    persisted features separate the lifecycle-tape survival outcomes, and does
    any apparent signal live in mature tape-backed evidence or fresh
    derived-only noise? Compute-on-demand; persists nothing; no external call;
    never advice. Returns tokens analyzed (in the selected cohort)."""
    from app.services.crypto_retrospect import build_retrospect_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_retrospect_report(session, hours=hours, top=top, cohort=cohort)
        print("crypto retrospective report — measurement only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h  cohort={r['cohort']}  "
            f"generated_at={r['generated_at']}"
            f"\ntokens_analyzed={r['tokens_analyzed']} (selected cohort)  "
            f"window_tokens={r['window_tokens']} "
            f"(tape_backed={r['tape_backed_tokens']}, "
            f"derived_only={r['derived_only_tokens']}"
            + (f", TRUNCATED at {r['universe_cap']}" if r["universe_truncated"] else "")
            + ")"
        )
        # CRYPTO-RETROSPECT-002: data source mix + maturity by source
        m = r["data_source_mix"]
        print(
            f"\ndata source mix: tape_backed={m['tape_backed']} "
            f"derived_only={m['derived_only']} immature(1h)={m['immature']}"
        )
        g = m["provider_gap_rate_by_source"]
        print(
            f"provider_gap rate by source: tape_backed={g['tape_backed']} "
            f"derived_only={g['derived_only']} all={g['all']}"
        )
        print("horizon maturity (known/unknown) by source:")
        for src in ("tape_backed", "derived_only", "all"):
            cov = m["horizon_coverage_by_source"][src]
            cells = "  ".join(
                f"{h.replace('survived_', '')}={cov[h]['known']}/{cov[h]['unknown']}"
                for h in ("survived_15m", "survived_1h", "survived_6h", "survived_24h")
            )
            print(f"  {src:<13} {cells}")
        # source stratification: where does any signal live?
        print("\nsource stratification (all | tape-backed | derived-only):")
        for s in r["source_stratification"]:
            def fmt(b):
                d = b["max_delta"]
                return f"{b['label']}" + (f"(Δ{d} on {b['driving_outcome']})" if d is not None else "")

            flag = "  ⚠ DILUTED" if s["diluted"] else ""
            print(
                f"  {s['dimension']:<22} [{s['source_label']}]{flag}\n"
                f"     all={fmt(s['all'])}  tape={fmt(s['tape_backed'])}  "
                f"derived={fmt(s['derived_only'])}"
            )
            if s["warning"]:
                print(f"     ! {s['warning']}")
        if r["diluted_dimensions"]:
            print(
                f"\n{len(r['diluted_dimensions'])} dimension(s) show a tape-backed "
                "signal HIDDEN in the all-window view — trust the tape-backed column."
            )
        print("\noutcome totals (true/false/unknown; unknown = immature or gap):")
        for outcome, mix in r["outcome_totals"].items():
            print(f"  {outcome:<22} {mix['true']}/{mix['false']}/{mix['unknown']}")
        if r["best_separators"]:
            print("best separators (max measured-cohort rate delta):")
            for s in r["best_separators"]:
                print(
                    f"  {s['dimension']:<24} {s['label']:<26} "
                    f"delta={s['max_delta']} on {s['driving_outcome']}"
                )
        if r["worst_separators"]:
            print("worst separators:")
            for s in r["worst_separators"]:
                print(
                    f"  {s['dimension']:<24} {s['label']:<26} "
                    f"delta={s['max_delta']} on {s['driving_outcome']}"
                )
        if r["unreadable_dimensions"]:
            print("unreadable dimensions (honest gaps):")
            for d in r["unreadable_dimensions"]:
                print(f"  {d['dimension']:<24} {d['label']:<26} {d['basis']}")
        print("\nper-dimension cohorts (n, survival_1h / liq_removed / dead_vol / severe / gap rates):")
        for dim in r["dimensions"]:
            interp = dim["interpretation"]
            print(f"  == {dim['dimension']} [{interp['label']}] {interp.get('basis', '')}")
            for c in dim["cohorts"][: max(top, 4)]:
                o = c["outcomes"]

                def fmt(name):
                    rate = o[name]["rate"]
                    return "n/a" if rate is None else f"{rate}"

                print(
                    f"     {c['cohort']:<20} n={c['n']:<4} [{c['label']}] "
                    f"surv1h={fmt('survived_1h')} liq_rm={fmt('liquidity_removed')} "
                    f"dead={fmt('dead_volume')} severe={fmt('severe_risk')} "
                    f"gap={fmt('provider_gap')}"
                )
                if c["label"] == "measured" and c["examples"]:
                    ex = c["examples"][0]
                    print(
                        f"       e.g. {ex['symbol'] or '?'} {ex['token']} "
                        f"{ex['outcomes'] or '(no true labels yet)'}"
                    )
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["tokens_analyzed"]
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        section("edge-precheck runtime")
        for key, value in report.edge_precheck_runtime.items():
            print(f"  {key}: {value}")
        section("recommended next action")
        print(report.recommended_next_action)

        if save_run:
            row = service.persist_run(session, report, started_at, hours)
            print(f"\nsaved eval run #{row.id}")
        return 0
    finally:
        if owns_session:
            session.close()


async def edge_followthrough_diagnostic_report(hours: int = 24, top: int = 5, session=None) -> int:
    """FOLLOWTHROUGH-001 read-only diagnostic: WHY is gap follow-through
    negative? Timing (signal/forecast/snapshot ages, pre-measurement market
    moves, gap-vs-move relation), direction (continued away vs reverted, spread/
    liquidity change), per-cohort verdicts, and concrete failure examples — all
    measured market movement over persisted rows. Not PnL, not EV, not advice;
    changes no gate/forecast/promotion/automation. Returns rows analyzed."""
    from app.services.edge_followthrough import EdgeFollowthroughDiagnosticService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = EdgeFollowthroughDiagnosticService().build(session, hours=hours, top=top)
        print(f"edge follow-through diagnostic (window {r['window_hours']}h) — analysis only, never advice")
        print(r["note"])
        o = r["overall"]
        print(
            f"\nrows={r['rows']}  final-horizon n={o['final_n']}  "
            f"toward_rate={o['toward_rate_final']}  mean_closure={o['mean_closure_final']}  "
            f"continued_away={o['continued_away_rate']}  reverted_toward={o['reverted_toward_rate']}"
        )
        print(
            f"timing: signal_age_p50={o['signal_age_p50_s']}s forecast_age_p50={o['forecast_age_p50_s']}s "
            f"snapshot_age_p50={o['snapshot_age_p50_s']}s  sharp_pre_move_share={o['sharp_pre_move_share']}  "
            f"gap_opposes_move_share={o['gap_opposes_move_share']}"
        )
        print(
            f"60m microstructure drift: spread_change_mean={o['spread_change_60m_mean']} "
            f"liquidity_change_mean={o['liquidity_change_60m_mean']}"
        )
        print(f"OVERALL VERDICT: {o['verdict']} — {o['verdict_reason']}")
        print("\nper-dimension verdicts (cohorts by sample size):")
        for dim, cohorts in r["dimensions"].items():
            print(f"  == {dim} ==")
            for key, c in cohorts.items():
                print(
                    f"    {str(key)[:26]:<26} n={c['n']:<4} final_n={c['final_n']:<4} "
                    f"toward={c['toward_rate_final']} closure={c['mean_closure_final']} "
                    f"opposes_move={c['gap_opposes_move_share']} -> {c['verdict']}"
                )
        fx = r["failure_examples"]
        print("\nfailure examples (measured movement — not opportunities):")
        print("  largest negative closure (final horizon):")
        for e in fx["largest_negative_closure"]:
            print(
                f"    #{e['snapshot_id']} {e['ticker'][:28]:<28} gap={e['gap']} "
                f"closure={e['closures']} pre_move={e['pre_move']} rel={e['gap_vs_pre_move']}"
            )
        if fx["repeated_ticker_failures"]:
            print("  repeated ticker failures (>=3 rows, none toward):")
            for e in fx["repeated_ticker_failures"]:
                print(f"    {e['ticker'][:30]:<30} rows={e['rows']} mean_closure={e['mean_closure_final']}")
        if fx["fresh_forecast_adverse"]:
            print("  fresh forecast (<120s) but adverse movement:")
            for e in fx["fresh_forecast_adverse"]:
                print(f"    #{e['snapshot_id']} {e['ticker'][:28]:<28} forecast_age={e['forecast_age_s']}s closure={e['closures']}")
        if fx["stale_snapshot_rows"]:
            print("  stale-ish market snapshot at measurement (>60s):")
            for e in fx["stale_snapshot_rows"]:
                print(f"    #{e['snapshot_id']} {e['ticker'][:28]:<28} snapshot_age={e['snapshot_age_s']}s")
        return r["rows"]
    finally:
        if owns_session:
            session.close()


async def trigger_timing_shadow_report(hours: int = 24, top: int = 5, session=None) -> int:
    """TRIGGER-TIMING-001 read-only SHADOW simulation: if edge-precheck had
    measured LATER (fixed cooldowns or settle-conditions), what would the gap
    and its follow-through have looked like? Replays persisted ticks with the
    recorded forecast held fixed. Changes no trigger/gate/forecast/promotion/
    automation; not PnL, not EV, never advice. Returns population size."""
    from app.services.trigger_timing import TriggerTimingShadowReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = TriggerTimingShadowReportService().build(session, hours=hours, top=top)
        print(f"trigger-timing shadow report (window {r['window_hours']}h) — analysis only, never advice")
        print(r["note"])
        print(f"\npopulation={r['population']} watchlist rows")
        print("\ntiming policies (measurable | opposes-share | 60m toward/closure | label):")
        for p in r["policies"]:
            ft60 = p["follow_through"].get("60m", {})
            print(
                f"  {p['name']:<32} n={p['rows_measurable']:<4} ({(p['survival_ratio'] or 0):>4.0%})  "
                f"opposes={p['gap_opposes_move_share']}  "
                f"60m toward={ft60.get('moved_toward_rate')} closure={ft60.get('mean_gap_closure_pct')}  "
                f"-> {p['label']}"
            )
        print("\nper-policy detail:")
        for p in r["policies"]:
            print(f"  == {p['name']} ({p['label']}) ==")
            print(f"     {p['label_reason']}")
            print(
                f"     measurable={p['rows_measurable']} final_n={p['final_n']} "
                f"lost={p['rows_lost']} median_delay_s={p['median_delay_s']}"
            )
            print(
                f"     sharp_pre_move={p['sharp_pre_move_share']} "
                f"market_type={p['market_type_mix']} gap_sign={p['gap_sign_mix']}"
            )
            print(
                "     follow: " + "  ".join(
                    f"{h}[n={v['samples']} toward={v['moved_toward_rate']} closure={v['mean_gap_closure_pct']}]"
                    for h, v in p["follow_through"].items()
                )
            )
            print(
                f"     paths: away={p['continued_away_rate']} toward={p['reverted_toward_rate']} "
                f"flat={p['flat_rate']}  drift60m: spread={p['spread_change_60m_mean']} "
                f"liq={p['liquidity_change_60m_mean']}"
            )
        ex = r["examples"]
        if ex["improved_by_delay_10m"]:
            print("\n  improved most by a 10m delay (closure delta vs immediate):")
            for e in ex["improved_by_delay_10m"][:3]:
                print(f"    {e['ticker'][:28]:<28} {e['baseline_closure_60m']} -> {e['delayed_closure_60m']} (delta {e['delta']})")
        if ex["worsened_by_delay_10m"]:
            print("  worsened most by a 10m delay:")
            for e in ex["worsened_by_delay_10m"][:3]:
                print(f"    {e['ticker'][:28]:<28} {e['baseline_closure_60m']} -> {e['delayed_closure_60m']} (delta {e['delta']})")
        print("\ncomparison (measurement only — never advice):")
        for k, v in r["comparison"].items():
            print(f"  {k}: {v}")
        return r["population"]
    finally:
        if owns_session:
            session.close()


async def edge_selection_validation_report(
    hours: int = 24,
    since: str | None = None,
    until: str | None = None,
    session=None,
) -> int:
    """EDGE-SELECTION-001 pre-registered validation report: evaluates ONLY the
    frozen policy registry (docs/EDGE_SELECTION_PREREG_2026_07_09.md) against
    fixed success/failure gates on an explicitly labelled discovery/validation
    window. Changes no gate/forecast/promotion/flag/automation; not PnL, not
    EV, never advice; validated_shadow authorizes nothing. Returns population."""
    from datetime import datetime, timezone

    from app.services.edge_selection import EdgeSelectionValidationReportService

    def _parse(ts: str | None):
        if ts is None:
            return None
        parsed = datetime.fromisoformat(ts)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = EdgeSelectionValidationReportService().build(
            session, hours=hours, since=_parse(since), until=_parse(until)
        )
        print("edge-selection validation report — pre-registered protocol, never advice")
        print(r["note"])
        w = r["window"]
        print(
            f"\nprereg: {r['prereg_doc']} (locked {r['prereg_locked_at']})"
            f"\nwindow: {w['start']} .. {w['end']}  type={w['type'].upper()}"
            f"  rows pre-lock={w['rows_pre_lock']} post-lock={w['rows_post_lock']}"
        )
        if w["type"] != "validation":
            print(
                "  NOTE: only a VALIDATION window (entirely after the lock) can "
                "validate a candidate — this window cannot."
            )
        print(f"population={r['population']} watchlist rows")
        print("\npre-registered policies (final_n | 60m toward/closure | max ticker/game share | status):")
        for p in r["policies"]:
            ft60 = p["follow_through"].get("60m", {})
            print(
                f"  {p['name']:<44} [{p['role']:<16}] n={p['final_n']:<4} "
                f"toward={ft60.get('moved_toward_rate')} closure={ft60.get('mean_gap_closure_pct')}  "
                f"conc={p['max_ticker_share']}/{p['max_game_share']}  -> {p['status']}"
            )
        print("\nper-policy gates:")
        for p in r["policies"]:
            print(f"  == {p['name']} ({p['role']}; alias {p['prereg_alias']}) ==")
            print(f"     status: {p['status']} — {p['status_reason']}")
            gates = "  ".join(f"{k}={'PASS' if v else 'fail'}" for k, v in p["gates"].items())
            print(f"     gates: {gates}")
            if p["failure_reasons"]:
                print(f"     failure gates tripped: {p['failure_reasons']}")
            print(f"     market_type_mix: {p['market_type_mix']}")
        print(
            f"\nnegative control consistent: {r['negative_control_consistent']}"
            f"\nvalidated_shadow policies this window: {r['validated_shadow_policies'] or 'none'}"
        )
        print(f"\noverfitting risk: {r['overfitting_note']}")
        print(f"\n{r['mvp_005b_note']}")
        return r["population"]
    finally:
        if owns_session:
            session.close()


async def edge_cost_shadow_report(hours: int = 24, top: int = 5, session=None) -> int:
    """COST-MODEL-001 read-only cost-adjusted SHADOW measurement: does any
    cohort's midpoint follow-through survive half-spread, a conservative
    Kalshi fee assumption, and executable touch prices? Changes no gate/
    forecast/promotion/flag/automation; not EV, not PnL, never advice.
    Returns population size."""
    from app.services.edge_cost import EdgeCostShadowReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = EdgeCostShadowReportService().build(session, hours=hours, top=top)
        print("edge-cost shadow report — cost-adjusted measurement only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h ({r['window_type']})  "
            f"fee_rate_assumption={r['fee_rate_assumption']} (round trip, conservative)"
            f"\npopulation={r['population']} watchlist rows  "
            f"measurable={r['rows_measurable']}  touch_coverage={r['touch_coverage']}"
        )
        print(
            "\ncohorts (n | cover | toward | frictionless / -half-spread / "
            "-fees / touch | label):"
        )
        for c in r["cohorts"]:
            print(
                f"  {c['name']:<44} [{c['dimension']:<20}] n={c['final_n']:<4} "
                f"cov={c['touch_coverage']}  toward={c['toward_rate_60m']}  "
                f"{c['frictionless_closure_60m']} / {c['net_closure_after_half_spread_60m']} / "
                f"{c['fee_adjusted_net_closure_60m']} / {c['executable_touch_closure_60m']}"
                f"  -> {c['label']}"
            )
        print("\nper-cohort detail:")
        for c in r["cohorts"]:
            print(f"  == {c['name']} ({c['label']}) ==")
            print(f"     {c['label_reason']}")
            print(
                f"     market_type_mix={c['market_type_mix']} "
                f"max_ticker_share={c['max_ticker_share']} "
                f"max_game_share={c['max_game_share']}"
            )
        print(
            f"\ncohorts positive after costs (fee-adjusted AND touch, n>=12): "
            f"{r['cohorts_positive_after_costs'] or 'NONE'}"
        )
        print(f"\n{r['mvp_005b_note']}")
        return r["population"]
    finally:
        if owns_session:
            session.close()


async def live_market_state_report(
    domain: str = "sports_tennis", top: int = 10, hours: int = 6, session=None
) -> int:
    """LIVE-MARKET-001 read-only live-state observation: freshness, quote
    quality, latency, and volatility DIAGNOSTICS for currently-ticking markets
    in a domain, plus the tennis match-winner state scaffold (persisted
    research packets only — provider gaps reported honestly, never
    fabricated). Changes no gate/forecast/promotion/flag/automation; not EV,
    not trading, never advice. Returns live-candidate count."""
    from app.services.live_market_state import LiveMarketStateReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = LiveMarketStateReportService().build(
            session, domain=domain, top=top, hours=hours
        )
        print("live market state report — observation only, never advice")
        print(r["note"])
        print(
            f"\ndomain={r['domain']}  window={r['window_hours']}h  "
            f"generated_at={r['generated_at']}"
            f"\nlive_candidates={r['live_candidates']}  "
            f"state_backed={r['state_backed_count']}  "
            f"template_only={r['template_only_count']}"
        )
        print(f"quote_quality_mix={r['quote_quality_mix']}")
        print(f"status_mix={r['status_mix']}")
        print(f"mean_market_freshness_s={r['mean_market_freshness_s']}")
        for gap in r["provider_gaps"]:
            print(f"  ! {gap}")
        for w in r["warnings"]:
            print(f"  ! {w}")
        if r["volatile_examples"]:
            print("\nvolatile markets (diagnostic labels, not signals):")
            for v in r["volatile_examples"]:
                print(
                    f"  {v['ticker'][:36]:<36} move_5m={v['move_5m']} "
                    f"move_10m={v['move_10m']}  ({v['reason']})"
                )
        print("\nobservations:")
        for o in r["observations"]:
            print(
                f"  == {o.market_ticker} [{o.market_type}] "
                f"status={o.market_status} =="
            )
            print(
                f"     mid={o.market_mid} bid={o.bid} ask={o.ask} "
                f"spread={o.spread} liq={o.liquidity}  "
                f"quote_quality={o.quote_quality}"
            )
            print(
                f"     freshness: market={o.market_freshness_s}s "
                f"score={o.score_freshness_s}s "
                f"score_to_market_lag={o.score_to_market_lag_s}s "
                f"moved_since_score={o.market_moved_since_last_score}"
            )
            print(
                f"     volatility: {o.volatility_label} ({o.volatility_reason}) "
                f"moves={o.moves} spread_d10m={o.spread_delta_10m} "
                f"instability={o.quote_instability_10m}"
            )
            print(
                f"     state_quality={o.state_quality}  "
                f"status={o.live_observation_status}"
            )
            if o.tennis:
                t = o.tennis
                print(
                    f"     tennis[{t['source']}]: {t['player_a']} vs {t['player_b']} "
                    f"sets={t['set_score']} games={t['game_score']} "
                    f"server={t['server']} match_status={t['match_status']}"
                )
                print(f"       missing_info={t['missing_info']}")
                print(f"       provenance={t['provenance']}")
            for w in o.warnings:
                print(f"     ! {w}")
        print(
            "\ndisclaimer: observation and diagnostics only — not EV, not a "
            "trade recommendation, no trading capability of any kind."
        )
        return r["live_candidates"]
    finally:
        if owns_session:
            session.close()


async def tennis_live_source_report(top: int = 10, hours: int = 24, session=None) -> int:
    """TENNIS-LIVE-SOURCE-001 read-only provider/source validation: can
    persisted tennis markets be mapped to source-backed live match state?
    Uses the existing TENNIS-001 provider scaffold only — with the default
    template provider nothing is fetched (honest provider_gap). Coverage
    measurement only; changes no gate/forecast/promotion/flag/automation;
    not EV, never advice. Returns total tennis market count."""
    from app.services.tennis_live_source import TennisLiveSourceReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await TennisLiveSourceReportService().build(session, top=top, hours=hours)
        print("tennis live-source validation report — coverage measurement only, never advice")
        print(r["note"])
        print(
            f"\nprovider={r['provider']}  window={r['window_hours']}h  "
            f"generated_at={r['generated_at']}"
        )
        print(
            f"total_tennis_markets={r['total_tennis_markets']}  "
            f"live_candidates={r['live_candidates']}  "
            f"match_winner_candidates={r['match_winner_candidates']}"
        )
        print(f"classification_mix={r['classification_mix']}")
        print(f"mapping_status_mix={r['mapping_status_mix']}")
        print(
            f"provider_match_rate={r['provider_match_rate']}  "
            f"source_backed={r['source_backed_count']}  "
            f"missing_player_mapping={r['missing_player_mapping_count']}  "
            f"unparseable_tickers={r['unparseable_ticker_count']}  "
            f"scoreboards_fetched={r['scoreboards_fetched']}"
        )
        for w in r["warnings"]:
            print(f"  ! {w}")
        print("\nexamples:")
        for c in r["examples"]:
            print(
                f"  == {c.market_ticker} [{c.market_classification}] "
                f"live={c.is_live_candidate} =="
            )
            print(
                f"     players={c.player_a}/{c.player_b} tour={c.tour} "
                f"date={c.event_date}  -> {c.mapping_status}"
            )
            print(
                f"     event_status={c.event_status} fetched_at={c.fetched_at} "
                f"quote_age_s={c.market_quote_age_s} "
                f"score_to_market_lag_s={c.score_to_market_lag_s}"
            )
            for note in c.notes:
                print(f"     . {note}")
        print(
            "\ndisclaimer: source-coverage validation only — no probability "
            "updates, not EV, not a recommendation, no trading capability."
        )
        return r["total_tennis_markets"]
    finally:
        if owns_session:
            session.close()


async def tennis_watch_scan_once(
    limit: int | None = None, hours: int = 24, dry_run: bool = False,
    scheduled: bool = False, session=None,
) -> int:
    """TENNIS-WATCHER-001 manual read-only tennis tick capture: one bounded
    quote pass over active tennis markets into market_price_ticks (same table
    and retention as the realtime watcher; no signals, no watcher_runs). Dry
    run persists nothing; the scheduled path no-ops unless
    ENABLE_TENNIS_TICK_WATCHER=true. Market observation only — not EV, never
    advice. Returns ticks recorded (0 for dry-run/skip)."""
    from app.services.tennis_watcher import TennisTickWatcher

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await TennisTickWatcher().scan_once(
            session, limit=limit, hours=hours, dry_run=dry_run, scheduled=scheduled
        )
        print("tennis tick scan — market observation only, never advice")
        print(f"status={r['status']}")
        if r.get("note"):
            print(r["note"])
        print(
            f"targets={r['targets']}  fetched={r['fetched']}  "
            f"two_sided_quotes={r.get('two_sided_quotes')}  "
            f"ticks_recorded={r['ticks_recorded']}"
        )
        if r.get("series_mix"):
            print(f"series_mix={r['series_mix']}")
        for entry in r.get("top_ordering") or []:
            print(f"  rank: {entry['ticker'][:44]:<44} {entry['reasons']}")
        return r["ticks_recorded"]
    finally:
        if owns_session:
            session.close()


async def tennis_watch_report(hours: int = 24, session=None) -> int:
    """TENNIS-WATCHER-001 read-only tennis tick-coverage report: active
    tennis markets vs tick-covered, freshness, quote completeness, series and
    market-type mixes. DB-only; no external call; changes nothing. Returns
    active tennis market count."""
    from app.services.tennis_watcher import build_tennis_watch_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_tennis_watch_report(session, hours=hours)
        print("tennis watch coverage report — market observation only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h  generated_at={r['generated_at']}  "
            f"ENABLE_TENNIS_TICK_WATCHER={r['flag_enable_tennis_tick_watcher']}"
        )
        print(
            f"active_tennis_markets={r['active_tennis_markets']}  "
            f"match_winner={r['match_winner_markets']}  "
            f"tick_covered={r['tick_covered']}  uncovered={r['uncovered']}  "
            f"coverage_rate={r['coverage_rate']}"
        )
        print(f"latest_tick_age_s={r['latest_tick_age_s']}")
        print(f"quote_stats={r['quote_stats']}")
        print(f"series_mix_active={r['series_mix_active']}")
        print(f"series_mix_covered={r['series_mix_covered']}")
        print(f"market_type_mix={r['market_type_mix']}")
        if r["uncovered_examples"]:
            print(f"uncovered_examples={r['uncovered_examples']}")
        print(f"\nprovider/state relationship: {r['provider_state_relationship']}")
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["active_tennis_markets"]
    finally:
        if owns_session:
            session.close()


async def tennis_tape_capture_once(
    limit: int | None = None, hours: int = 24, dry_run: bool = False, session=None,
) -> int:
    """TENNIS-TAPE-001 manual bounded tape capture: one score pass (hard call
    cap) + one market quote pass + linking, persisting ONLY tape rows (dry-run
    persists nothing). Measurement infrastructure — not a model, not EV, never
    advice. Returns links created (0 for dry-run/skip)."""
    from app.services.tennis_tape import TennisTapeRecorder

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await TennisTapeRecorder().capture_once(
            session, limit=limit, hours=hours, dry_run=dry_run
        )
        print("tennis tape capture — measurement only, never advice")
        print(f"status={r['status']}")
        if r.get("note"):
            print(r["note"])
        print(
            f"candidates={r['candidates']}  score_calls={r['score_calls']}  "
            f"market_fetches={r['market_fetches']}  "
            f"quotes_returned={r.get('quotes_returned')}  "
            f"two_sided={r.get('two_sided_quotes')}"
        )
        print(f"links={r['links']}")
        for entry in r.get("top_ordering") or []:
            print(f"  rank: {entry['ticker'][:44]:<44} {entry['reasons']}")
        print(
            f"persisted: score_snapshots={r['score_snapshots']} "
            f"market_snapshots={r['market_snapshots']}"
            + (f"  tape_run_id={r['tape_run_id']}" if r.get("tape_run_id") else "")
        )
        return sum(r["links"].values()) if r["links"] else 0
    finally:
        if owns_session:
            session.close()


async def tennis_tape_capture_session(
    duration_min: int = 15, interval_sec: int = 90, limit: int | None = None,
    dry_run: bool = False, session=None,
) -> int:
    """TENNIS-CAPTURE-SESSION-001 bounded manual capture session: a fixed,
    capped number of capture_once passes in ONE invocation, then exit — not a
    timer, not a daemon. Aborts on abnormal capture status or detectable
    MarketOps error. Measurement only; never advice. Returns captures run."""
    from app.services.tennis_tape import run_capture_session

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await run_capture_session(
            session, duration_min=duration_min, interval_sec=interval_sec,
            limit=limit, dry_run=dry_run,
        )
        print("tennis tape capture session — measurement only, never advice")
        print(
            f"status={r['status']}"
            + (f"  ABORT: {r['abort_reason']}" if r["abort_reason"] else "")
        )
        print(
            f"duration_min={r['duration_min']}  interval_sec={r['interval_sec']}  "
            f"captures={r['captures_run']}/{r['captures_planned']}  "
            f"provider_calls={r['provider_calls']}"
        )
        print(f"capture_statuses={r['capture_statuses']}")
        s = r["session_summary"]
        if s.get("available"):
            print(
                f"session: runs={s['runs']}  score_snapshots={s['score_snapshots']}  "
                f"market_snapshots={s['market_snapshots']}  links={s['links']}"
            )
            print(f"quote_coverage={s['quote_coverage']}  db_impact_rows={s['db_impact_rows']}")
            if s["top_movers"]:
                print("top moving markets (abs mid range across session):")
                for m in s["top_movers"]:
                    print(
                        f"  {m['ticker'][:44]:<44} {m['first_mid']} -> {m['last_mid']} "
                        f"(range {m['abs_range']})"
                    )
        else:
            print(f"session summary: {s['reason']}")
        return r["captures_run"]
    finally:
        if owns_session:
            session.close()


async def tennis_tape_report(hours: int = 24, top: int = 5, session=None) -> int:
    """TENNIS-TAPE-001 tape report: runs, snapshot volumes, link quality,
    freshness, score-to-market deltas, examples. DB-only; read-only; never
    advice. Returns tape run count."""
    from app.services.tennis_tape import build_tape_report

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = build_tape_report(session, hours=hours, top=top)
        print("tennis tape report — measurement only, never advice")
        print(r["note"])
        print(
            f"\nwindow={r['window_hours']}h  generated_at={r['generated_at']}"
            f"\ntape_runs={r['tape_runs']}  score_snapshots={r['score_snapshots']}  "
            f"market_snapshots={r['market_snapshots']}  links={r['links_total']}"
        )
        print(
            f"link_label_mix={r['link_label_mix']}  "
            f"source_backed_rate={r['source_backed_rate']}"
        )
        print(
            f"quote_coverage={r['quote_coverage']}  "
            f"in_play_score_snapshots={r['in_play_score_snapshots']}"
        )
        print(
            f"freshness: score={r['score_freshness_s']}s "
            f"market={r['market_freshness_s']}s  "
            f"mean_score_to_market_delta_s={r['mean_score_to_market_delta_s']}"
        )
        for gap in r["provider_gaps"]:
            print(f"  ! {gap}")
        if r["linked_examples"]:
            print("linked examples:")
            for e in r["linked_examples"]:
                print(f"  {e['ticker'][:44]:<44} event={e['event_id']} delta_s={e['delta_s']}")
        if r["unresolved_examples"]:
            print("unresolved/no-match examples:")
            for e in r["unresolved_examples"]:
                print(f"  {e['ticker'][:44]:<44} [{e['label']}] {e['basis']}")
        print(f"db_impact_rows={r['db_impact_rows']}")
        print(f"\ndisclaimer: {r['disclaimer']}")
        return r["tape_runs"]
    finally:
        if owns_session:
            session.close()


async def tennis_api_livefeed_probe(
    duration_sec: int = 60, top: int = 10, session=None,
) -> int:
    """TENNIS-LIVE-FEED-002 bounded WebSocket live-feed validation: does the
    provider emit usable live ITF/Challenger state? Connects only with the
    host-only key (never printed), fixed duration, persists nothing, REST
    comparison included. Not a model, not EV, never advice. Returns matched
    candidate count (>=0)."""
    from app.services.tennis_livefeed import TennisLiveFeedProbe

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await TennisLiveFeedProbe().probe(
            session, duration_sec=duration_sec, top=top
        )
        print("tennis live-feed probe — provider validation only, never advice")
        print(r["note"])
        print(
            f"\nprovider={r['provider_tested']}  url={r['ws_display_url']}  "
            f"duration={r['duration_sec']}s  generated_at={r['generated_at']}"
        )
        print(
            f"live_candidates={r['live_candidates']}  ws_frames={r['ws_frames']}  "
            f"ws_events={r['ws_events']}  unparseable={r['ws_unparseable_frames']}  "
            f"distinct_matches={r['distinct_matches']}"
        )
        print(
            f"state_changes={r['state_changes']}  "
            f"matched_candidates={r['matched_candidates']}"
        )
        if r["connection_error"]:
            print(f"  ! connection_error={r['connection_error']}")
        if r["matched_examples"]:
            print("matched candidates:")
            for e in r["matched_examples"]:
                print(f"  {e['ticker'][:44]:<44} status={e['status']}")
        if r["state_change_examples"]:
            print("state-change examples:")
            for e in r["state_change_examples"]:
                print(
                    f"  {e['players'][:44]:<44} versions={e['versions']} "
                    f"{e['first_status']} -> {e['last_status']} [{e['type']}]"
                )
        print(f"rest_comparison={r['rest_comparison']}")
        print(f"\nVERDICT: {r['verdict']}")
        print(f"recommendation: {r['recommendation']}")
        return r["matched_candidates"]
    finally:
        if owns_session:
            session.close()


async def tennis_goalserve_probe(
    probes: int = 2, interval_sec: int = 20, top: int = 10, session=None,
) -> int:
    """TENNIS-GOALSERVE-001 bounded Goalserve live-state validation under the
    exact conditions that failed API-Tennis: same candidates, same linker,
    hard call cap, nothing persisted, key never in any printed URL. Not a
    model, not EV, never advice. Returns matched candidate count."""
    from app.services.tennis_goalserve import GoalserveValidationService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = await GoalserveValidationService().validate(
            session, probes=probes, interval_sec=interval_sec, top=top
        )
        print("goalserve live-state validation — provider validation only, never advice")
        print(r["note"])
        print(
            f"\nprovider={r['provider_tested']}  url={r['display_url']}"
            f"\ngenerated_at={r['generated_at']}  live_candidates={r['live_candidates']}"
        )
        print(
            f"probes={r['probes_planned']}  calls_made={r['calls_made']}  "
            f"fetch_errors={r['fetch_errors'] or 'none'}"
        )
        print(
            f"live_rows_per_probe={r['live_rows_per_probe']}  "
            f"in_play_per_probe={r['in_play_rows_per_probe']}"
        )
        print(
            f"state_changes={r['state_changes']}  "
            f"live_state_fields={r['live_state_fields']}"
        )
        print(f"matched_candidates={r['matched_candidates']}")
        if r["matched_examples"]:
            print("matched ITF/Challenger candidates:")
            for e in r["matched_examples"]:
                print(
                    f"  {e['ticker'][:44]:<44} status={e['status']} "
                    f"in_play={e['in_play']} sets={e['sets']}"
                )
        if r["miss_examples"]:
            print("provider misses:")
            for e in r["miss_examples"][:5]:
                print(f"  {e['ticker'][:44]:<44} [{e['label']}]")
        if r["state_change_examples"]:
            print("state-change examples:")
            for e in r["state_change_examples"]:
                print(
                    f"  {e['players'][:40]:<40} status={e['status']} "
                    f"sets={e['sets']} points={e['point_score']}"
                )
        print(f"api_tennis_baseline: {r['api_tennis_baseline']}")
        print(f"\nVERDICT: {r['verdict']}")
        print(f"recommendation: {r['recommendation']}")
        return r["matched_candidates"]
    finally:
        if owns_session:
            session.close()


async def edge_selection_retirement_report(session=None) -> int:
    """EDGE-RETIRE-001 experiment-registry report: the frozen retirement
    record (discovery vs out-of-sample vs cost) plus the CURRENT post-lock
    behavior of the retired policies (observation only). Changes nothing;
    never advice. Returns retired-candidate count."""
    from app.services.edge_selection import (
        PREREG_LOCKED_AT,
        RETIRED_AT,
        RETIRED_CANDIDATES,
        RETIREMENT_CONCLUSION,
        RETIREMENT_DOC,
        EdgeSelectionValidationReportService,
    )

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        print("edge-selection retirement report — experiment registry only, never advice")
        print(f"retirement record: {RETIREMENT_DOC} (retired_at={RETIRED_AT})")
        print(f"prereg lock: {PREREG_LOCKED_AT.isoformat()}")
        print("\nretired candidates (frozen record — discovery vs out-of-sample, 60m toward/closure):")
        for name, rec in RETIRED_CANDIDATES.items():
            print(f"  {name:<44} discovery={rec['discovery']:<12} validation={rec['validation']}")
        print(f"\nconclusion: {RETIREMENT_CONCLUSION}")
        print("\ncurrent post-lock behavior (live re-measurement — registry observation only):")
        r = EdgeSelectionValidationReportService().build(
            session, since=PREREG_LOCKED_AT
        )
        print(f"  window: {r['window']['start']} .. {r['window']['end']}  type={r['window']['type']}")
        print(f"  population={r['population']}")
        for p in r["policies"]:
            ft60 = p["follow_through"].get("60m", {})
            retired = " [RETIRED]" if p.get("retired") else ""
            print(
                f"  {p['name']:<44} n={p['final_n']:<4} "
                f"toward={ft60.get('moved_toward_rate')} "
                f"closure={ft60.get('mean_gap_closure_pct')} -> {p['status']}{retired}"
            )
        print(f"\n{r['mvp_005b_note']}")
        print(
            "resurrection rule: a retired policy is ineligible for live "
            "gate/paper/MVP regardless of future windows; a NEW prereg + NEW "
            "lock is required for any successor hypothesis."
        )
        return len(RETIRED_CANDIDATES)
    finally:
        if owns_session:
            session.close()


async def forecast_anchor_diagnostic_report(hours: int = 24, top: int = 5, session=None) -> int:
    """FORECAST-ANCHOR-001 read-only diagnostic: when the market moved between
    the PRIOR forecast and this measurement, did the forecast move too? Per-row
    adjustment ratios + anchor buckets, cohort verdicts, and interpretation —
    all from recorded forecasts and ticks. Changes no forecast/gate/promotion/
    automation; not PnL, not EV, never advice. Returns rows analyzed."""
    from app.services.forecast_anchor import ForecastAnchorDiagnosticService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = ForecastAnchorDiagnosticService().build(session, hours=hours, top=top)
        print(f"forecast anchoring diagnostic (window {r['window_hours']}h) — analysis only, never advice")
        print(r["note"])
        o = r["overall"]
        print(
            f"\nrows={r['rows']}  classified={o['classified_n']}  "
            f"unclassifiable_share={o['unclassifiable_share']}"
        )
        print(f"anchor buckets: {o['bucket_counts']}")
        print(f"bucket shares (of classified): {o['bucket_shares']}")
        print(
            f"deltas: median_forecast={o['median_forecast_delta']} "
            f"median_market={o['median_market_delta']} "
            f"median_adjustment_ratio={o['median_adjustment_ratio']} "
            f"market_moved_more_share={o['market_moved_more_share']}"
        )
        print("follow-through by anchor bucket (60m):")
        for b, ft in o["follow_through_by_bucket"].items():
            print(f"  {b:<22} n={ft['n']:<4} toward={ft['toward_rate_60m']} closure={ft['mean_closure_60m']}")
        print(f"OVERALL VERDICT: {o['verdict']} — {o['verdict_reason']}")
        print("\nper-dimension verdicts:")
        for dim, cohorts in r["dimensions"].items():
            print(f"  == {dim} ==")
            for key, c in cohorts.items():
                shares = c["bucket_shares"]
                anchored = round((shares.get("anchored_static") or 0) + (shares.get("partial_adjustment") or 0), 3)
                print(
                    f"    {str(key)[:24]:<24} n={c['n']:<4} classified={c['classified_n']:<4} "
                    f"anchored+partial={anchored} ratio={c['median_adjustment_ratio']} "
                    f"toward60={c['toward_rate_60m']} -> {c['verdict']}"
                )
        ex = r["examples"]
        print("\nexamples (measured values — never advice):")
        for section, items in ex.items():
            if not items:
                continue
            print(f"  {section}:")
            for e in items[:3]:
                print(
                    f"    {e['ticker'][:28]:<28} [{e['bucket']}] f_delta={e['forecast_delta']} "
                    f"m_delta={e['market_delta']} ratio={e['adjustment_ratio']} c60={e['closure_60m']}"
                )
        print("\ninterpretation (measurement only — never advice):")
        for k, v in r["interpretation"].items():
            print(f"  {k}: {v}")
        return r["rows"]
    finally:
        if owns_session:
            session.close()


async def edge_filter_shadow_report(hours: int = 24, top: int = 5, session=None) -> int:
    """EDGE-FILTER-001 read-only SHADOW filter analysis: what would the
    watchlist's follow-through have looked like under candidate adverse-
    selection filters (gap-vs-move, sharp pre-move, market type, series)?
    Re-slices existing rows only — changes no live gate/forecast/promotion/
    automation; not PnL, not EV, never advice. Returns population size."""
    from app.services.edge_filter_shadow import EdgeFilterShadowReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        r = EdgeFilterShadowReportService().build(session, hours=hours, top=top)
        print(f"edge shadow-filter report (window {r['window_hours']}h) — analysis only, never advice")
        print(r["note"])
        print(f"\npopulation={r['population']} watchlist rows  worst_series={r['worst_series']}")
        print("\npolicies (survival | 60m toward/closure | label):")
        for p in r["policies"]:
            ft60 = p["follow_through"].get("60m", {})
            print(
                f"  {p['name']:<44} kept={p['included']:<4} ({(p['survival_ratio'] or 0):>5.0%})  "
                f"60m toward={ft60.get('moved_toward_rate')} closure={ft60.get('mean_gap_closure_pct')}  "
                f"-> {p['label']}"
            )
        print("\nper-policy detail:")
        for p in r["policies"]:
            print(f"  == {p['name']} ({p['label']}) ==")
            print(f"     {p['label_reason']}")
            print(
                f"     included={p['included']} excluded={p['excluded']} final_n={p['final_n']}  "
                f"paths: away={p['continued_away_rate']} toward={p['reverted_toward_rate']} flat={p['flat_rate']}"
            )
            print(f"     market_type={p['market_type_mix']}  gap_sign={p['gap_sign_mix']}")
            print(f"     signal_type={p['signal_type_mix']}")
            print(f"     series={p['series_mix']}  max_ticker_share={p['max_ticker_share']} max_game_share={p['max_game_share']}")
            print(
                f"     follow: " + "  ".join(
                    f"{h}[n={v['samples']} toward={v['moved_toward_rate']} closure={v['mean_gap_closure_pct']}]"
                    for h, v in p["follow_through"].items()
                )
            )
            print(
                f"     drift60m: spread={p['spread_change_60m_mean']} liquidity={p['liquidity_change_60m_mean']}"
            )
            if p["examples_removed"]:
                print("     removed (worst first): " + "; ".join(
                    f"{e['ticker'][:24]} rel={e['relation']} c60={e['closure_60m']}"
                    for e in p["examples_removed"][:3]
                ))
            if p["examples_retained"]:
                print("     retained (best first): " + "; ".join(
                    f"{e['ticker'][:24]} rel={e['relation']} c60={e['closure_60m']}"
                    for e in p["examples_retained"][:3]
                ))
        print("\ninterpretation (measurement only — never advice):")
        for k, v in r["interpretation"].items():
            print(f"  {k}: {v}")
        return r["population"]
    finally:
        if owns_session:
            session.close()


async def edge_cohort_report(hours: int = 24, session=None) -> int:
    """Print the edge-precheck cohort analysis (analysis only — no EV, no
    trades, no positions; cohort labels authorize nothing). Returns 0."""
    from app.services.edge_cohort import EdgeCohortReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


async def backup_db(dry_run: bool = False) -> int:
    """Create a verified, compressed, timestamped SQLite backup (consistent
    snapshot via the online backup API), publish it atomically only after
    verification, then apply bounded tiered retention. Returns 0 on success;
    a bounded skip (overlap/capacity) returns 0 without creating anything."""
    from app.services.backup import BackupResult, backup_database

    result = backup_database(dry_run=dry_run)
    if isinstance(result, str):
        print(result)  # non-SQLite guidance; nothing executed
        return 1
    assert isinstance(result, BackupResult)
    print("crypto/db backup — read-only snapshot of our own database, never advice")
    print(f"status={result.status} external_calls=0")
    if result.detail:
        print(f"  {result.detail}")
    if result.status == "dry_run":
        print("  dry-run: no backup created, nothing deleted")
        return 0
    if result.status == "skipped_overlap":
        # transient and self-healing on the next cycle; not a unit failure
        print("  skipped: not a successful backup; nothing created or deleted")
        return 0
    if result.status in ("skipped_capacity", "skipped_contention"):
        # persistent, human-actionable conditions. Exiting 0 here would leave the
        # timer green while backups silently stop happening, so fail the unit.
        print("  skipped: not a successful backup; nothing created or deleted")
        return 75  # EX_TEMPFAIL
    if result.status == "failed_verification":
        return 1
    print(f"backup written: {result.path} ({result.size_bytes / (1024 * 1024):.2f} MiB)")
    print(f"  manifest: {result.manifest_path}")
    print(f"  sha256={result.sha256}")
    print(f"  backup_ms={result.duration_ms} verification_ms={result.verification_ms}")
    for name in result.pruned:
        print(f"  pruned old backup: {name}")
    return 0


async def prune_db_backups(confirm: bool = False) -> int:
    """Apply the bounded tiered backup-retention policy. Without --confirm this
    only reports the plan; the newest backup is never deleted, and only
    strictly-named files confined to the backup root are ever removed."""
    from app.services.backup import apply_retention

    plan = apply_retention(dry_run=not confirm)
    print("db backup retention — bounded tiered policy, newest always retained")
    print(f"mode={'confirmed' if confirm else 'dry_run'} external_calls=0")
    print(f"retained={len(plan.retained)} deleted={len(plan.deleted)} "
          f"skipped={len(plan.skipped)}")
    for name in plan.retained:
        print(f"  retain: {name}")
    for name in plan.deleted:
        print(f"  {'deleted' if confirm else 'would delete'}: {name}")
    for name in plan.skipped:
        print(f"  skip (not a managed backup): {name}")
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
    from app.config import get_settings
    from app.services.backup import _backup_dir, verify_backup

    # decompress beside the backup root, never the default (root-volume) TMPDIR
    tmp_dir = _backup_dir(get_settings())
    result = verify_backup(path, tmp_dir=tmp_dir if tmp_dir.is_dir() else None)
    print(f"{'OK' if result.ok else 'FAILED'}: {result.detail}")
    return 0 if result.ok else 1


def sqlite_backup_freshness_report(fmt: str = "text") -> dict:
    """SQLITE-BACKUP-FRESHNESS-ALERT-001 — is the canonical backup root still
    holding a recent, committed, structurally valid, manifest-backed backup?

    Read-only in the strongest sense: it makes zero provider calls, opens no
    database, writes no file, creates no alert, executes no backup and prunes
    nothing. It inspects local backup files and manifests only, and does not
    recompute the artifact SHA-256 (that is `verify-db-backup`'s job).

    There is deliberately no `--backup-root` / `--now` flag: the evaluator takes
    both as injectable arguments for tests, and exposing an arbitrary
    operator-supplied path on a production CLI is a strictly larger surface than
    this check needs. Text and JSON report exactly the same fields.
    """
    import json as _json

    from app.services.backup_freshness import (
        BACKUP_FRESHNESS_MAX_AGE_SECONDS,
        evaluate_backup_freshness,
    )

    result = evaluate_backup_freshness()
    data = result.to_dict()

    if fmt == "json":
        print(_json.dumps(data, indent=2, sort_keys=True))
        return data

    print("sqlite backup freshness — local manifest-backed backup health "
          "(read-only; zero calls, no writes)")
    print(f"status={data['status']} healthy={str(data['healthy']).lower()} "
          f"reason={data['reason']}")
    print(f"evaluated_at={data['evaluated_at']}")
    print(f"threshold_seconds={BACKUP_FRESHNESS_MAX_AGE_SECONDS} "
          f"({BACKUP_FRESHNESS_MAX_AGE_SECONDS / 3600:.0f}h)")
    print(f"backup_root={data['backup_root']}")
    print(f"backup_root: configured={str(data['backup_root_configured']).lower()} "
          f"exists={str(data['backup_root_exists']).lower()} "
          f"is_directory={str(data['backup_root_is_directory']).lower()} "
          f"is_symlink={str(data['backup_root_is_symlink']).lower()}")
    print(f"manifests: strict={data['strict_manifest_count']} "
          f"invalid={data['invalid_manifest_count']}")
    print(f"newest_manifest_filename={data['newest_manifest_filename']}")
    print(f"newest_backup_filename={data['newest_backup_filename']}")
    print(f"newest_verified_at={data['newest_verified_at']} "
          f"age_seconds={data['age_seconds']}")
    print(f"artifact: exists={str(data['artifact_exists']).lower()} "
          f"regular_file={str(data['artifact_is_regular_file']).lower()} "
          f"is_symlink={str(data['artifact_is_symlink']).lower()} "
          f"bytes={data['artifact_bytes']} "
          f"manifest_bytes={data['manifest_backup_bytes']} "
          f"size_matches_manifest={str(data['size_matches_manifest']).lower()}")
    print(f"manifest: version={data['manifest_version']} "
          f"status={data['manifest_status']} "
          f"integrity_check={data['manifest_integrity_check']} "
          f"alembic_revision={data['manifest_alembic_revision']}")
    print(f"external_calls={data['external_calls']} "
          f"duration_ms={data['duration_ms']}")
    print("full sha256 verification is NOT performed here — use verify-db-backup")
    return data


async def crypto_risk_assess(
    limit: int = 50,
    engine=None,
    session=None,
    *,
    provider_plan: bool = False,
    allow_provider: list | None = None,
    deny_provider: list | None = None,
    confirm_paid_provider: list | None = None,
    yes: bool = False,
) -> int:
    """Assess risk for recently-seen tokens from persisted Crypto Arena data
    (heuristics always; providers when their flags are on), governed by an
    explicit provider policy (GATE-001). A bare invocation prints the zero-call
    provider plan and does NOT execute; execution requires --yes plus
    per-provider confirmation for any paid provider. Read-only risk intelligence
    — a score is an avoid/flag verdict, never a trade recommendation. Returns
    the number of tokens assessed (0 when only the plan is printed)."""
    from sqlalchemy import select

    from app.models import CryptoPair, CryptoPriceTick, CryptoToken
    from app.services.crypto_provider_policy import (
        provider_run,
        render_ledger,
        render_provider_plan,
        resolve_cli_policy,
    )
    from app.services.crypto_risk_engine import RISK_SIGNAL_TYPES, CryptoRiskEngine
    from app.services.crypto_scout import CryptoSignalService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        engine = engine or CryptoRiskEngine()
        descriptors = engine.registry.describe(limit)
        resolution = resolve_cli_policy(
            descriptors,
            allow=allow_provider or [],
            deny=deny_provider or [],
            confirm_paid=confirm_paid_provider or [],
        )
        if provider_plan or not yes:
            print(render_provider_plan("crypto-risk-assess", descriptors, resolution))
            if not yes:
                return 0
        if resolution.policy is None:
            print(render_provider_plan("crypto-risk-assess", descriptors, resolution))
            print("aborted: provider plan is blocked; no provider was contacted.")
            return 0
        signal_service = CryptoSignalService()
        assessed = 0
        risk_signals = 0
        with provider_run(resolution.policy) as ctx:
            tokens = session.execute(
                select(CryptoToken)
                .order_by(CryptoToken.last_seen_at.desc(), CryptoToken.id.desc())
                .limit(limit)
            ).scalars().all()
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
                        .order_by(
                            CryptoPriceTick.observed_at.desc(), CryptoPriceTick.id.desc()
                        )
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
            print(
                f"assessed {assessed} token(s), created {risk_signals} risk signal(s)"
            )
            print(render_ledger(ctx.ledger.snapshot()))
        return assessed
    finally:
        if owns_session:
            session.close()


async def crypto_risk_report(session=None) -> int:
    """Print the aggregate crypto risk report. Returns tokens assessed."""
    from app.services.crypto_risk_engine import CryptoRiskReportService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        if report.db_growth is not None:
            dbg = report.db_growth
            print(
                f"database growth: {dbg.get('status')} "
                f"size_mb={dbg.get('size_mb')} "
                f"warning_mb={dbg.get('warning_mb')} "
                f"critical_mb={dbg.get('critical_mb')} "
                f"alert={dbg.get('alert_action')}"
            )
        if report.backup_freshness is not None:
            bfr = report.backup_freshness
            print(
                f"backup protection: {bfr.get('status')} "
                f"reason={bfr.get('reason')} "
                f"newest={bfr.get('newest_backup_filename')} "
                f"age_seconds={bfr.get('age_seconds')}/"
                f"{bfr.get('threshold_seconds')} "
                f"alert={bfr.get('alert_action')}"
            )
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


def marketops_reconcile_db_growth_alerts(
    confirm: bool = False, fmt: str = "text", session=None
) -> dict:
    """DB-GROWTH-ALERT-IDENTITY-001 — converge the legacy database-growth alert
    backlog onto ONE canonical row.

    Dry-run by default and read-only in that mode: it writes nothing, calls no
    provider, deletes no application data, runs no backup or retention, and
    touches no cohort or observation. `--confirm` performs exactly one bounded,
    atomic operational-alert reconciliation — duplicates are RESOLVED through
    the existing lifecycle, never hard-deleted, so alert history is preserved.
    """
    import json as _json

    from app.services.marketops import (
        ALERT_DB_GROWTH as ALERT_TYPE_DB_GROWTH,
    )
    from app.services.marketops import reconcile_db_growth_alerts

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
        session = get_sessionmaker()()
    try:
        report = reconcile_db_growth_alerts(session, confirm=confirm)
    finally:
        if owns_session:
            session.close()

    if fmt == "json":
        print(_json.dumps(report, indent=2, sort_keys=True))
        return report

    mode = "CONFIRMED" if report["confirmed"] else "DRY RUN"
    print(f"marketops db-growth alert reconciliation — {mode}")
    print(f"persisted={str(report['persisted']).lower()} "
          f"external_calls={report['external_calls']} "
          f"hard_deletes={report['hard_deletes']}")
    print(f"database: size_mb={report['size_mb']} "
          f"warning_mb={report['warning_mb']} critical_mb={report['critical_mb']} "
          f"status={report['status']} severity={report['severity']}")
    print(f"canonical_title={report['canonical_title']!r}")
    print(f"legacy_title_pattern={report['legacy_title_pattern']!r}")
    print(f"open db_growth rows: total={report['open_total']} "
          f"matched={report['matched_total']} "
          f"(canonical={report['matched_canonical']} legacy={report['matched_legacy']}) "
          f"already_resolved={report['already_resolved']}")
    print(f"excluded (unmatched titles, never written): "
          f"{report['excluded_unmatched']} {report['excluded_unmatched_titles']}")
    print(f"canonical row: id={report['canonical_id']} "
          f"source={report['canonical_source']} "
          f"created_at={report['canonical_created_at']}")
    print(f"condition_first_observed_at={report['condition_first_observed_at']}")
    verb = "resolved" if report["persisted"] else "would resolve"
    print(f"{verb}: {report.get('resolved', report['would_resolve'])} "
          f"id_range={report['would_resolve_id_range']}")
    print(f"remaining open after: {report['remaining_open_after']} "
          f"(matched={report['remaining_open_matched_after']} "
          f"unmatched={report['remaining_open_unmatched_after']})")
    if report["persisted"]:
        print(f"resolved_at={report['resolved_at']}  "
              f"(inverse: UPDATE marketops_alerts SET status='open', "
              f"resolved_at=NULL WHERE alert_type='{ALERT_TYPE_DB_GROWTH}' "
              f"AND resolved_at='{report['resolved_at']}')")
    if not report["confirmed"]:
        print("nothing was written — re-run with --confirm to apply")
    return report


async def marketops_resolve_alert(alert_id: int, session=None) -> int:
    """Resolve one alert by id. Returns 0 on success, 1 when not found."""
    from app.services.marketops import MarketOpsAlertService

    owns_session = session is None
    if owns_session:
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()

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
        from app.db import ensure_schema_current, get_sessionmaker

        ensure_schema_current()
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


def _add_provider_gate_args(sub) -> None:
    """CRYPTO-DISCOVERY-PROVIDER-GATE-001 shared governance flags."""
    sub.add_argument(
        "--provider-plan", action="store_true",
        help="print the zero-call provider plan and exit (no request issued)",
    )
    sub.add_argument(
        "--allow-provider", action="append", metavar="PROVIDER", default=[],
        help="restrict the run to these providers (repeatable)",
    )
    sub.add_argument(
        "--deny-provider", action="append", metavar="PROVIDER", default=[],
        help="explicitly deny a provider — overrides flags/allow/fallback (repeatable)",
    )
    sub.add_argument(
        "--confirm-paid-provider", action="append", metavar="PROVIDER", default=[],
        help="explicitly confirm a specific paid provider (required to call it)",
    )
    sub.add_argument(
        "--yes", action="store_true",
        help="execute the plan (without this, only the plan is printed)",
    )


def build_parser() -> argparse.ArgumentParser:
    from app.services.crypto_sparse_observation import (
        DEFAULT_REPORT_HOURS as OBS_COVERAGE_DEFAULT_HOURS,
    )

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
    fsa_parser = subparsers.add_parser(
        "forecast-scorability-audit-report",
        help="Read-only forecast scorability + representativeness diagnostics "
             "(FORECAST-SCORABILITY-AUDIT-001; zero calls, no writes)",
    )
    fsa_parser.add_argument("--hours", type=int, default=None,
                            help="window = now-hours .. now (over forecast created_at)")
    fsa_parser.add_argument("--since", type=str, default=None,
                            help="ISO UTC window start (overrides --hours)")
    fsa_parser.add_argument("--until", type=str, default=None, help="ISO UTC window end (default now)")
    fsa_parser.add_argument("--domain", type=str, default=None)
    fsa_parser.add_argument("--forecaster", type=str, default=None)
    fsa_parser.add_argument("--top", type=int, default=10)
    fsa_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")
    erv = subparsers.add_parser(
        "experiment-registry-validate",
        help="Validate a prospective experiment manifest (PROSPECTIVE-EXPERIMENT-"
             "REGISTRY-001; pure, no writes, no provider calls)")
    erv.add_argument("--manifest", type=str, required=True)
    erv.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    err = subparsers.add_parser(
        "experiment-registry-register",
        help="Register a manifest immutably (dry run unless --confirm)")
    err.add_argument("--manifest", type=str, required=True)
    err.add_argument("--confirm", action="store_true")
    err.add_argument("--dry-run", action="store_true")
    err.add_argument("--base", type=str, default=None,
                     help="registry root (defaults to ./experiments)")
    err.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    krr = subparsers.add_parser(
        "kalshi-realtime-replay",
        help="Deterministically replay an archived Kalshi event stream "
             "(KALSHI-REALTIME-OBSERVATION-001A; read-only, zero calls)")
    krr.add_argument("--archive", type=str, required=True)
    krr.add_argument("--environment", choices=("demo", "production"),
                     default="demo")
    krr.add_argument("--format", choices=("text", "json"), default="text",
                     dest="fmt")

    ai = subparsers.add_parser(
        "archive-init",
        help="Explicitly initialize an archive root -- the ONLY way a "
             "genesis is minted (KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7). "
             "Dry run unless --confirm.")
    ai.add_argument("--root", type=str, required=True)
    ai.add_argument("--environment", choices=("demo", "production"),
                    default="demo")
    ai.add_argument("--archive-identity", type=str, default="kalshi-realtime",
                    dest="archive_identity")
    ai.add_argument("--confirm", action="store_true")
    ai.add_argument("--dry-run", action="store_true")
    ai.add_argument("--format", choices=("text", "json"), default="text",
                    dest="fmt")

    arh = subparsers.add_parser(
        "archive-recover-head",
        help="Finish an interrupted archive-head transition "
             "(KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7); deterministic, "
             "idempotent, safe to run when nothing is wrong. Dry run "
             "unless --confirm.")
    arh.add_argument("--root", type=str, required=True)
    arh.add_argument("--environment", choices=("demo", "production"),
                     default="demo")
    arh.add_argument("--confirm", action="store_true")
    arh.add_argument("--dry-run", action="store_true")
    arh.add_argument("--format", choices=("text", "json"), default="text",
                     dest="fmt")

    aad = subparsers.add_parser(
        "archive-adopt",
        help="Commit ONE orphaned committed segment into the archive's "
             "authoritative history (KALSHI-ARCHIVE-CORE-REMEDIATION-002 "
             "A7); bounded to segments verify_archive itself reports as "
             "orphaned. Never discards evidence. Dry run unless --confirm.")
    aad.add_argument("--root", type=str, required=True)
    aad.add_argument("--segment-id", type=str, required=True, dest="segment_id")
    aad.add_argument("--environment", choices=("demo", "production"),
                     default="demo")
    aad.add_argument("--confirm", action="store_true")
    aad.add_argument("--dry-run", action="store_true")
    aad.add_argument("--format", choices=("text", "json"), default="text",
                     dest="fmt")

    aml = subparsers.add_parser(
        "archive-migrate-legacy",
        help="Explicit offline import of a pre-genesis legacy archive into "
             "a NEW governed archive (KALSHI-ARCHIVE-CORE-REMEDIATION-002 "
             "A7); the legacy source is never written, moved or deleted. "
             "Dry run unless --confirm.")
    aml.add_argument("--source", type=str, required=True)
    aml.add_argument("--dest", type=str, required=True)
    aml.add_argument("--environment", choices=("demo", "production"),
                     default="demo")
    aml.add_argument("--archive-identity", type=str, default="kalshi-realtime",
                     dest="archive_identity")
    aml.add_argument("--confirm", action="store_true")
    aml.add_argument("--dry-run", action="store_true")
    aml.add_argument("--format", choices=("text", "json"), default="text",
                     dest="fmt")

    errr = subparsers.add_parser(
        "experiment-registry-record-result",
        help="Registry-owned evaluation of a registered experiment "
             "(REGISTRY-002B; dry run unless --confirm)")
    errr.add_argument("--experiment-id", type=str, required=True)
    errr.add_argument("--confirm", action="store_true")
    errr.add_argument("--dry-run", action="store_true")
    errr.add_argument("--notes", type=str, default=None,
                      help="non-authoritative operator prose")
    errr.add_argument("--reevaluation-reason", type=str, default=None)
    errr.add_argument("--base", type=str, default=None)
    errr.add_argument("--format", choices=("text", "json"), default="text",
                      dest="fmt")

    erp = subparsers.add_parser(
        "experiment-registry-report",
        help="Full read-only experiment report (identity, predicates, drift, "
             "integrity); fails closed on tampering")
    erp.add_argument("--experiment-id", type=str, required=True)
    erp.add_argument("--base", type=str, default=None)
    erp.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    ers = subparsers.add_parser("experiment-registry-status",
                                help="Show one experiment's state and integrity")
    ers.add_argument("--experiment-id", type=str, required=True)
    ers.add_argument("--base", type=str, default=None)
    ers.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    erl = subparsers.add_parser("experiment-registry-list",
                                help="List registered experiments")
    erl.add_argument("--base", type=str, default=None)
    erl.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    ert = subparsers.add_parser("experiment-registry-transition",
                                help="Advance experiment state (dry run unless --confirm)")
    ert.add_argument("--experiment-id", type=str, required=True)
    ert.add_argument("--to", type=str, required=True)
    ert.add_argument("--confirm", action="store_true")
    ert.add_argument("--dry-run", action="store_true")
    ert.add_argument("--note", type=str, default=None)
    ert.add_argument("--base", type=str, default=None)
    ert.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    osc_parser = subparsers.add_parser(
        "outcome-sync-coverage-report",
        help="Read-only outcome-coverage diagnostics and missing-outcome taxonomy "
             "(OUTCOME-SYNC-COVERAGE-001; zero calls, no writes)",
    )
    osc_parser.add_argument("--hours", type=int, default=None,
                            help="window = now-hours .. now (over forecast created_at)")
    osc_parser.add_argument("--since", type=str, default=None,
                            help="ISO UTC window start (overrides --hours)")
    osc_parser.add_argument("--until", type=str, default=None, help="ISO UTC window end (default now)")
    osc_parser.add_argument("--domain", type=str, default=None)
    osc_parser.add_argument("--examples", type=int, default=3,
                            help="bounded examples per missing reason")
    osc_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    osb_parser = subparsers.add_parser(
        "outcome-sync-backfill",
        help="Bounded historical outcome synchronization through the existing "
             "read-only Kalshi detail path (OUTCOME-SYNC-COVERAGE-001)",
    )
    osb_parser.add_argument("--confirm", action="store_true",
                            help="actually fetch and persist; omit for a dry run")
    osb_parser.add_argument("--dry-run", action="store_true",
                            help="force a dry run even with --confirm")
    osb_parser.add_argument("--max-markets", type=int, default=250,
                            help="hard provider-call cap for this run")
    osb_parser.add_argument("--domain", type=str, default=None)
    osb_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")

    frd_parser = subparsers.add_parser(
        "forecast-reliability-decomposition-report",
        help="Read-only calibration/reliability decomposition over scored_current "
             "forecasts (FORECAST-RELIABILITY-DECOMP-001; zero calls, no writes)",
    )
    frd_parser.add_argument("--hours", type=int, default=None)
    frd_parser.add_argument("--since", type=str, default=None, help="ISO UTC (overrides --hours)")
    frd_parser.add_argument("--until", type=str, default=None, help="ISO UTC (default now)")
    frd_parser.add_argument("--domain", type=str, default=None)
    frd_parser.add_argument("--forecaster", type=str, default=None)
    frd_parser.add_argument("--bins", type=int, default=10, help="equal-width probability bins (>=2)")
    frd_parser.add_argument("--minimum-bin-count", type=int, default=None)
    frd_parser.add_argument("--minimum-cohort-count", type=int, default=None)
    frd_parser.add_argument("--top", type=int, default=10)
    frd_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")
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
    do_reclaim_parser = subparsers.add_parser(
        "raw-payload-reclaim",
        help="Replace eligible HISTORICAL raw bodies with the canonical "
             "suppressed envelope (RAW-PAYLOAD-RECLAMATION-001). Dry run "
             "without --confirm. Target must be a reviewed registry name.",
    )
    do_reclaim_parser.add_argument("target", type=str)
    do_reclaim_parser.add_argument("--confirm", action="store_true")
    do_reclaim_parser.add_argument("--dry-run", action="store_true")
    do_reclaim_parser.add_argument("--max-rows", type=int, default=None)
    do_reclaim_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
    reclaim_parser = subparsers.add_parser(
        "raw-payload-reclamation-report",
        help="Read-only dry run for historical raw-payload reclamation "
             "(RAW-PAYLOAD-RECLAMATION-001): eligible rows, bytes, preservation "
             "floors and blockers. Writes nothing; there is no --confirm here.",
    )
    reclaim_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
    raw_payload_parser = subparsers.add_parser(
        "raw-payload-storage-report",
        help="Read-only raw provider-payload storage measurement "
             "(RAW-PAYLOAD-STORAGE-001): capture mode, rows by capture state, "
             "bytes stored and avoided. Writes nothing; prints no payload.",
    )
    raw_payload_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
    raw_payload_parser.add_argument(
        "--recent", type=int, default=5000,
        help="inspect the newest N rows per governed column "
             "(primary-key ordered and bounded; the timestamp "
             "columns are not indexed)",
    )
    raw_payload_parser.add_argument(
        "--full-scan", action="store_true",
        help="also compute whole-table totals; this reads every page of the "
             "payload columns and can block the writer — use on a backup copy",
    )
    coverage_parser = subparsers.add_parser(
        "retention-coverage-report",
        help="Read-only retention coverage analysis (RETENTION-COVERAGE-001): "
             "size, growth, windows, readers, preservation floors and eligible "
             "rows. Deletes nothing; there is no --confirm.",
    )
    coverage_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
    coverage_parser.add_argument("--top", type=int, default=25)
    coverage_parser.add_argument(
        "--no-dbstat", action="store_true",
        help="skip per-table page attribution (avoids a full-database read "
             "scan; prefer this on a large production database)",
    )
    prune_parser.add_argument("--dry-run", action="store_true")
    prune_parser.add_argument("--tick-days", type=int, default=None)
    prune_parser.add_argument("--watcher-run-days", type=int, default=None)
    prune_parser.add_argument("--pipeline-run-days", type=int, default=None)
    prune_parser.add_argument("--signal-days", type=int, default=None)
    prune_parser.add_argument("--batch-size", type=int, default=None)
    subparsers.add_parser("db-stats", help="Print database overview and row counts")
    subparsers.add_parser(
        "db-schema-report",
        help="Print the resolved DB path, current/head Alembic revision, and "
             "SCHEMA_MATCH/SCHEMA_BEHIND_CODE/SCHEMA_AHEAD_OF_CODE status "
             "(read-only; exit 0 iff SCHEMA_MATCH)",
    )
    subparsers.add_parser(
        "db-integrity-check",
        help="Run PRAGMA integrity_check via this app's own resolved "
             "database (replaces the runbook's bare `sqlite3` invocation); "
             "prints the resolved path and fails if it is empty/unexpected",
    )
    growth_parser = subparsers.add_parser(
        "db-growth-report",
        help="OPS-011: DB size/growth, tick age+domain buckets, retention windows, alert thresholds",
    )
    growth_parser.add_argument("--top", type=int, default=12, help="largest N tables to list")
    agg_parser = subparsers.add_parser(
        "aggregate-market-ticks",
        help="OPS-012: roll raw ticks into fixed OHLC buckets (idempotent; never deletes raw ticks; not advice)",
    )
    agg_parser.add_argument("--hours", type=int, default=24)
    agg_parser.add_argument(
        "--bucket-seconds", type=int, default=None,
        help="bucket interval (default from settings, 300s; must divide 3600)",
    )
    agg_parser.add_argument("--dry-run", action="store_true", help="compute + report, write nothing")
    agg_parser.add_argument("--max-rows", type=int, default=None, help="raw-row cap for this pass")
    agg_parser.add_argument(
        "--subwindow-hours", type=int, default=None,
        help="commit after each sub-window of this many hours (default from settings, 1)",
    )
    agg_parser.add_argument(
        "--scheduled", action="store_true",
        help="timer mode: refuse unless ENABLE_TICK_AGGREGATION_TIMER=true",
    )
    subparsers.add_parser(
        "tick-aggregation-report",
        help="OPS-012: aggregation coverage + staged (not enacted) retention recommendation",
    )
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
    _add_provider_gate_args(crypto_scan_parser)
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
    _add_provider_gate_args(crypto_risk_parser)
    subparsers.add_parser("crypto-risk-report", help="Aggregate crypto risk report")
    subparsers.add_parser(
        "crypto-provider-health-report",
        help="Crypto risk provider coverage/health (explicit gaps; read-only)",
    )
    subparsers.add_parser(
        "crypto-provider-budget-report",
        help="SolanaTracker request accounting + budget status (read-only cost/usage)",
    )
    tape_run_parser = subparsers.add_parser(
        "crypto-tape-run-once",
        help="One derived lifecycle-tape assembly pass (CRYPTO-TAPE-001; "
             "no external calls, never advice)",
    )
    tape_run_parser.add_argument("--limit", type=int, default=None)
    tape_run_parser.add_argument("--hours", type=int, default=None)
    tape_run_parser.add_argument(
        "--dry-run", action="store_true", help="compute and report; persist nothing"
    )
    tape_run_parser.add_argument(
        "--source-crypto-run-id", type=int, default=None,
        help="ANCHOR-FEED-MEASUREMENT-001 exact-run mode: consolidate EXACTLY "
             "the tokens first persisted by this crypto discovery run (no "
             "freshest fallback; rejects --limit/--hours; requires --confirm "
             "to persist)",
    )
    tape_run_parser.add_argument(
        "--confirm", action="store_true",
        help="exact-run mode only: actually persist (absent = preview only)",
    )
    tape_reconcile_parser = subparsers.add_parser(
        "crypto-tape-reconcile",
        help="CRYPTO-COVERAGE-REPAIR-001: one bounded provider-free "
             "reconciliation pass over tokens whose horizons have matured "
             "(default OFF via enable_crypto_tape_reconciler; never advice)",
    )
    tape_reconcile_parser.add_argument("--hours", type=int, default=None)
    tape_reconcile_parser.add_argument("--limit", type=int, default=None)
    tape_reconcile_parser.add_argument(
        "--dry-run", action="store_true", help="compute and report; persist nothing"
    )
    tape_reconcile_parser.add_argument(
        "--force", action="store_true",
        help="run a single pass even when the scheduling flag is off "
             "(manual operator use; does not enable the timer)",
    )
    tape_reconcile_parser.add_argument(
        "--batch-size", type=_positive_int_arg, default=None,
        help="HIGH-2/NEW-HIGH-2: override crypto_tape_reconciler_batch_size "
             "for this one invocation (tokens committed per batch). Must be "
             ">= 1 (argparse-validated) and, at runtime, < the selection "
             "limit — batch_size >= limit collapses the pass back into a "
             "single monolithic transaction.",
    )
    tape_reconcile_parser.add_argument(
        "--max-duration-seconds", type=_non_negative_float_arg, default=None,
        help="HIGH-2/LOW-3: override crypto_tape_reconciler_max_duration_seconds "
             "for this one invocation. Needed to measure a FULL, untruncated "
             "dry run on a host where the default 20s deadline is too tight "
             "for the actual per-pass work — without this flag --dry-run is "
             "chunked at the same 20s deadline as a real pass and can itself "
             "report dry_run_partial. Must be >= 0 (0.0 intentionally means "
             "'already past due' — stop after exactly one batch).",
    )
    tape_reconcile_parser.add_argument(
        "--time-budget-seconds", type=_positive_float_arg, default=None,
        help="CRYPTO-COVERAGE-REPAIR-001 B1/B3: the write-time SLO adaptive "
             "batching sizes each transaction against. Only takes effect "
             "together with --initial-per-token-cost-seconds (adaptive "
             "batching is otherwise off); defaults to "
             "RECONCILE_WRITE_TIME_SLO_SECONDS when adaptive mode is active "
             "and this flag is omitted. Must be > 0.",
    )
    tape_reconcile_parser.add_argument(
        "--initial-per-token-cost-seconds", type=_positive_float_arg, default=None,
        help="CRYPTO-COVERAGE-REPAIR-001 B1/B3: an explicit, MEASURED "
             "per-token write-phase wall-clock cost (seconds) on THIS host. "
             "Supplying this ACTIVATES adaptive time-budgeted batching, "
             "which replaces --batch-size as the safety invariant "
             "(--batch-size then becomes only a sanity maximum). There is "
             "deliberately no default — this value is UNCALIBRATED until an "
             "operator has actually measured a real batch's write-phase "
             "wall time on the target host (see crypto_tape.py's "
             "AdaptiveBatchCostEstimate); never guess it. Must be > 0.",
    )
    tape_reconcile_parser.add_argument(
        "--lock-wait-budget-seconds", type=_positive_float_arg, default=None,
        help="CRYPTO-RECONCILER-LOCK-WAIT-BUDGET-001: a CAP (seconds) on how "
             "long ONE SQLite lock acquisition may WAIT. Distinct from "
             "--time-budget-seconds, which bounds how long a transaction may "
             "HOLD the lock. Omitted (the default) means the budget is "
             "derived per attempt from the pass's own remaining "
             "--max-duration-seconds, so a blocked statement can never push "
             "the pass past its deadline. Must be > 0.",
    )
    reconciler_health_parser = subparsers.add_parser(
        "crypto-reconciler-health",
        help="CRYPTO-RECONCILER-GUARDED-TIMER-001: show the recurring "
             "reconciler's unattended health gate and its auto-disable latch "
             "(read-only unless --clear; never advice)",
    )
    reconciler_health_parser.add_argument(
        "--clear", action="store_true",
        help="clear a TRIPPED latch so scheduled runs may resume. Requires "
             "--operator. Nothing clears the latch automatically — that is "
             "the point of it.",
    )
    reconciler_health_parser.add_argument(
        "--operator", type=str, default=None,
        help="the human accountable for the clear (recorded in the latch "
             "history); required with --clear",
    )
    reconciler_health_parser.add_argument(
        "--note", type=str, default=None,
        help="what was checked before clearing (recorded in the latch history)",
    )
    tape_report_parser = subparsers.add_parser(
        "crypto-tape-report",
        help="Lifecycle tape report: coverage, survival labels, actor patterns "
             "(read-only)",
    )
    tape_report_parser.add_argument("--hours", type=int, default=24)
    tape_report_parser.add_argument("--top", type=int, default=5)
    tape_session_parser = subparsers.add_parser(
        "crypto-tape-session",
        help="Bounded manual tape session to mature survival horizons "
             "(CRYPTO-TAPE-CADENCE-001; not a timer, zero external calls)",
    )
    tape_session_parser.add_argument("--duration-hours", type=int, default=6)
    tape_session_parser.add_argument("--interval-min", type=int, default=30)
    tape_session_parser.add_argument("--limit", type=int, default=None)
    tape_session_parser.add_argument(
        "--dry-run", action="store_true",
        help="print the planned schedule + one dry probe; persist nothing",
    )
    hcc_parser = subparsers.add_parser(
        "crypto-horizon-cohort-create",
        help="Freeze a fixed research cohort for horizon observation "
             "(CRYPTO-HORIZON-OBS-001; read-only selection)",
    )
    hcc_parser.add_argument("--limit", type=int, default=25)
    hcc_parser.add_argument("--hours", type=int, default=48)
    hcc_parser.add_argument("--dry-run", action="store_true",
                            help="preview selection; persist nothing")
    hcc_parser.add_argument(
        "--require-complete", action="store_true",
        help="select only complete-lifecycle-anchor births (pair + positive "
             "initial liquidity + price); excludes null-liquidity fresh tokens",
    )
    hcc_parser.add_argument(
        "--min-liquidity", type=float, default=0.0,
        help="minimum initial liquidity (USD) when --require-complete is set",
    )
    hcc_parser.add_argument(
        "--token", action="append", metavar="CANONICAL_TOKEN_ID", default=[],
        help="COHORT-SELECT-002: freeze EXACTLY these canonical token id(s) "
             "(repeatable; explicit mode — no freshest-first fallback)",
    )
    hcc_parser.add_argument(
        "--confirm", action="store_true",
        help="explicit mode: atomically persist the exact requested cohort",
    )
    hcc_parser.add_argument(
        "--require-shared-horizon-windows", action="store_true",
        help="explicit mode: require a nonempty shared valid window per horizon "
             "(shared-pass canary); reject otherwise",
    )
    hobs_parser = subparsers.add_parser(
        "crypto-horizon-observe-once",
        help="One manual bounded observation pass over due horizons "
             "(CRYPTO-HORIZON-OBS-001; DexScreener only, no timer/loop)",
    )
    hobs_parser.add_argument("--cohort-id", type=int, required=True)
    hobs_parser.add_argument("--limit", type=int, default=25)
    hobs_parser.add_argument("--dry-run", action="store_true",
                             help="plan preview; ZERO external calls, nothing persisted")
    hsched_parser = subparsers.add_parser(
        "crypto-horizon-schedule-report",
        help="Static manual timing schedule for cohort horizons "
             "(CRYPTO-HORIZON-SCHEDULE-001; zero calls, no persistence)",
    )
    hsched_parser.add_argument("--cohort-id", type=int, required=True)
    hsched_parser.add_argument(
        "--top", type=int, default=None,
        help="limit detailed output to the first N cohort members",
    )
    hrem_parser = subparsers.add_parser(
        "crypto-horizon-reminder-plan",
        help="Static deduplicated reminder plan; installs and invokes nothing",
    )
    hrem_parser.add_argument("--cohort-id", type=int, required=True)
    harm_parser = subparsers.add_parser(
        "crypto-horizon-arm-cohort",
        help="Plan or explicitly install bounded one-shot user-systemd jobs",
    )
    harm_parser.add_argument("--cohort-id", type=int, required=True)
    harm_parser.add_argument(
        "--dry-run", action="store_true",
        help="print exact jobs; zero calls, persistence, or installation",
    )
    harm_parser.add_argument(
        "--confirm", action="store_true",
        help="explicitly install the reviewed one-shot jobs on EVO-X2",
    )
    hrun_parser = subparsers.add_parser(
        "crypto-horizon-run-job",
        help="Internal one-attempt worker for an installed horizon job",
    )
    hrun_parser.add_argument("--cohort-id", type=int, required=True)
    hrun_parser.add_argument("--job-id", type=int, required=True)
    horch_parser = subparsers.add_parser(
        "crypto-horizon-orchestrator-report",
        help="Report planned/installed/completed one-shot horizon jobs",
    )
    horch_parser.add_argument("--cohort-id", type=int, required=True)
    hdisarm_parser = subparsers.add_parser(
        "crypto-horizon-disarm-cohort",
        help="Preview or remove only one cohort's horizon one-shot units",
    )
    hdisarm_parser.add_argument("--cohort-id", type=int, required=True)
    hdisarm_parser.add_argument(
        "--confirm", action="store_true",
        help="explicitly remove the listed cohort-specific units",
    )
    hfeas_parser = subparsers.add_parser(
        "crypto-horizon-shared-candidate-feasibility-report",
        help="Local shared-candidate feasibility for CANARY-004 "
             "(CRYPTO-HORIZON-SHARED-CANDIDATE-FEASIBILITY-001; zero calls, no writes)",
    )
    hfeas_parser.add_argument("--hours", type=int, default=None,
                              help="add an N-hour funnel range")
    hfeas_parser.add_argument("--days", type=int, default=None,
                              help="add an N-day funnel range")
    hfeas_parser.add_argument("--limit", type=int, default=10,
                              help="cap examples and per-segment rows")
    hfeas_parser.add_argument("--arm-margin-seconds", type=float, default=0.0,
                              help="operator/install/verify margin beyond the 45s activation grace")
    hfeas_parser.add_argument("--format", choices=("text", "json"), default="text",
                              dest="fmt")
    hready_parser = subparsers.add_parser(
        "crypto-horizon-candidate-readiness-report",
        help="Current shared-pass candidate readiness from local data "
             "(CRYPTO-HORIZON-CANDIDATE-READINESS-001; zero calls, no writes)",
    )
    hready_parser.add_argument("--at", type=str, default=None,
                               help="evaluate at this ISO UTC instant (default: now)")
    hready_parser.add_argument("--hours", type=int, default=None,
                               help="reserved; readiness is evaluated at a single instant")
    hready_parser.add_argument("--limit", type=int, default=5)
    hready_parser.add_argument("--require-complete", action="store_true", default=True,
                               help="always applied to the live verdict (cannot be relaxed)")
    hready_parser.add_argument("--minimum-arm-margin-seconds", type=float, default=None,
                               help="operator margin beyond the 45s grace (default 180s)")
    hready_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")
    hreadyhist_parser = subparsers.add_parser(
        "crypto-horizon-candidate-readiness-history-report",
        help="Aggregate accumulated readiness evaluations "
             "(CRYPTO-HORIZON-CANDIDATE-READINESS-001; read-only)",
    )
    hreadyhist_parser.add_argument("--limit", type=int, default=20)
    hreadyhist_parser.add_argument("--format", choices=("text", "json"), default="text", dest="fmt")
    hrep_parser = subparsers.add_parser(
        "crypto-horizon-observation-report",
        help="Horizon-observation coverage report + success gates "
             "(CRYPTO-HORIZON-OBS-001; read-only)",
    )
    hrep_parser.add_argument("--cohort-id", type=int, required=True)
    hrep_parser.add_argument("--top", type=int, default=5)
    hrep_parser.add_argument("--shadow", action="store_true",
                             help="pre-observation coverage-gain + provider-load estimate")
    hpsr_parser = subparsers.add_parser(
        "crypto-horizon-pair-selection-report",
        help="Diagnose failed (no-liquidity) observations + shadow pair policies "
             "(CRYPTO-HORIZON-OBS-002; read-only)",
    )
    hpsr_parser.add_argument("--cohort-id", type=int, required=True)
    hpsr_parser.add_argument("--top", type=int, default=5)
    horr_parser = subparsers.add_parser(
        "crypto-horizon-outcome-reconciliation-report",
        help="Prove observation -> outcome unknown->known transition "
             "(CRYPTO-HORIZON-OBS-002; read-only, nothing persisted)",
    )
    horr_parser.add_argument("--cohort-id", type=int, required=True)
    horr_parser.add_argument("--top", type=int, default=5)
    cov_parser = subparsers.add_parser(
        "crypto-tape-coverage-report",
        help="Coverage forensics: why survival horizons stay unmeasurable + "
             "shadow selection analysis (CRYPTO-COVERAGE-001; diagnostic only)",
    )
    cov_parser.add_argument("--hours", type=int, default=168)
    cov_parser.add_argument("--top", type=int, default=5)
    cov_parser.add_argument(
        "--limit", type=int, default=25,
        help="the recorder's per-run token cap to model in the shadow analysis",
    )
    sparse_parser = subparsers.add_parser(
        "crypto-sparse-observe",
        help="CRYPTO-COVERAGE-REPAIR-002: one bounded prospective "
             "sparse-observation pass — enrol eligible new births and buy "
             "exactly one 6h and one 24h DexScreener observation each "
             "(default OFF via enable_crypto_sparse_observation; no "
             "SolanaTracker; never advice)",
    )
    sparse_parser.add_argument(
        "--dry-run", action="store_true",
        help="report exactly what would be enrolled and observed; ZERO "
             "external calls, nothing enrolled, nothing written",
    )
    sparse_parser.add_argument(
        "--force", action="store_true",
        help="run a single pass even when the scheduling flag is off "
             "(manual operator use; does not enable the timer)",
    )
    sparse_parser.add_argument(
        "--enrol-limit", type=_positive_int_arg, default=None,
        help="override crypto_sparse_observation_enrol_limit for this "
             "invocation (births admitted to the standing cohort per pass)",
    )
    sparse_parser.add_argument(
        "--observe-limit", type=_positive_int_arg, default=None,
        help="override crypto_sparse_observation_observe_limit for this "
             "invocation. This IS the per-pass DexScreener request cap and "
             "may never exceed the horizon lane's hard OBSERVE_MAX_CALLS=100",
    )
    sparse_parser.add_argument(
        "--write-batch-size", type=_positive_int_arg, default=None,
        help="tokens committed per write transaction in the write phase "
             "(the fetch phase holds no transaction at all)",
    )
    sparse_parser.add_argument(
        "--max-duration-seconds", type=_non_negative_float_arg, default=None,
        help="wall-clock deadline on the FETCH phase; a pass that stops here "
             "reports status=partial/stop_reason=deadline and the remaining "
             "member-horizons stay selectable while their band is open",
    )
    obscov_parser = subparsers.add_parser(
        "crypto-observation-coverage-report",
        help="CRYPTO-COVERAGE-REPAIR-002: OBSERVATION coverage — did a "
             "scheduled look happen inside its band. NOT reconciliation "
             "coverage (see crypto-tape-coverage-report); reads no survival "
             "label, persists nothing, zero external calls",
    )
    # B6: the window is BOUNDED by default. Unbounded (`--all`) is one dense
    # column scan of the whole observation table — measured at year 1 on a 754MB
    # fixture it stalls a co-tenant writer 3.0-3.7s, and EVO's database is
    # 4.55GB with documented load overshoot. Full history is still available and
    # still correct; it is now an explicit choice.
    obscov_parser.add_argument(
        "--hours", type=int, default=None,
        help=f"restrict to members enrolled within the last N hours "
             f"(default: {OBS_COVERAGE_DEFAULT_HOURS})",
    )
    obscov_parser.add_argument(
        "--all", action="store_true", dest="all_history",
        help="every enrolled member, no window. Scans the whole observation "
             "table and can stall a concurrent writer for seconds on a large "
             "database — use deliberately, not as a default",
    )
    obscov_parser.add_argument("--top", type=int, default=5)
    retro_parser = subparsers.add_parser(
        "crypto-retrospect-report",
        help="Retrospective feature/outcome separation analysis "
             "(CRYPTO-RETROSPECT-001; compute-on-demand, never advice)",
    )
    retro_parser.add_argument("--hours", type=int, default=48)
    retro_parser.add_argument("--top", type=int, default=5)
    retro_parser.add_argument(
        "--cohort", choices=["all", "tape-backed", "derived-only"], default="all",
        help="re-lens the headline to a token source (default all); the source "
             "stratification section is always shown",
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
    mo_reconcile_parser = subparsers.add_parser(
        "marketops-reconcile-db-growth-alerts",
        help="Converge the legacy database-growth alert backlog onto one "
             "canonical row (DB-GROWTH-ALERT-IDENTITY-001; dry-run by default, "
             "resolves duplicates, never deletes, zero provider calls)",
    )
    mo_reconcile_parser.add_argument(
        "--dry-run", action="store_true",
        help="report scope only and write nothing (default behaviour)",
    )
    mo_reconcile_parser.add_argument(
        "--confirm", action="store_true",
        help="apply the reconciliation in one atomic transaction",
    )
    mo_reconcile_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
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
    ft_diag_parser = subparsers.add_parser(
        "edge-followthrough-diagnostic-report",
        help="FOLLOWTHROUGH-001: WHY is gap follow-through negative? timing/direction/verdicts/examples (read-only; never advice)",
    )
    ft_diag_parser.add_argument("--hours", type=int, default=24)
    ft_diag_parser.add_argument("--top", type=int, default=5, help="failure examples per section")
    ef_shadow_parser = subparsers.add_parser(
        "edge-filter-shadow-report",
        help="EDGE-FILTER-001: shadow adverse-selection filters over existing watchlist rows (read-only; changes nothing; never advice)",
    )
    ef_shadow_parser.add_argument("--hours", type=int, default=24)
    ef_shadow_parser.add_argument("--top", type=int, default=5, help="examples per policy")
    fa_parser = subparsers.add_parser(
        "forecast-anchor-diagnostic-report",
        help="FORECAST-ANCHOR-001: did the forecast move when the market moved? anchor buckets/ratios/verdicts (read-only; never advice)",
    )
    fa_parser.add_argument("--hours", type=int, default=24)
    fa_parser.add_argument("--top", type=int, default=5, help="examples per section")
    tt_parser = subparsers.add_parser(
        "trigger-timing-shadow-report",
        help="TRIGGER-TIMING-001: shadow simulation of delayed/settle-gated measurement over persisted ticks (read-only; never advice)",
    )
    tt_parser.add_argument("--hours", type=int, default=24)
    tt_parser.add_argument("--top", type=int, default=5, help="examples per section")
    es_parser = subparsers.add_parser(
        "edge-selection-validation-report",
        help="EDGE-SELECTION-001: pre-registered policy validation vs fixed gates on labelled discovery/validation windows (read-only; never advice)",
    )
    es_parser.add_argument("--hours", type=int, default=24)
    es_parser.add_argument(
        "--since", type=str, default=None,
        help="ISO window start (UTC assumed if naive); overrides --hours",
    )
    es_parser.add_argument(
        "--until", type=str, default=None, help="ISO window end (default: now)"
    )
    subparsers.add_parser(
        "edge-selection-retirement-report",
        help="EDGE-RETIRE-001: frozen retirement record + current post-lock behavior of retired policies (registry only; never advice)",
    )
    ec_parser = subparsers.add_parser(
        "edge-cost-shadow-report",
        help="COST-MODEL-001: cost-adjusted follow-through — spread/fee/executable-touch friction over existing rows (read-only; never advice)",
    )
    ec_parser.add_argument("--hours", type=int, default=24)
    ec_parser.add_argument("--top", type=int, default=5, help="series cohorts shown")
    lms_parser = subparsers.add_parser(
        "live-market-state-report",
        help="LIVE-MARKET-001: live-state observation — freshness/quote/latency/volatility diagnostics + tennis scaffold (read-only; never advice)",
    )
    lms_parser.add_argument("--domain", type=str, default="sports_tennis")
    lms_parser.add_argument("--top", type=int, default=10)
    lms_parser.add_argument("--hours", type=int, default=6, help="tick recency window for live candidacy")
    tls_parser = subparsers.add_parser(
        "tennis-live-source-report",
        help="TENNIS-LIVE-SOURCE-001: can persisted tennis markets map to source-backed live state? provider coverage validation (read-only; never advice)",
    )
    tls_parser.add_argument("--top", type=int, default=10)
    tls_parser.add_argument("--hours", type=int, default=24, help="recency window for live candidacy")
    tws_parser = subparsers.add_parser(
        "tennis-watch-scan-once",
        help="TENNIS-WATCHER-001: one bounded read-only tennis tick capture pass (market observation only; never advice)",
    )
    tws_parser.add_argument("--limit", type=int, default=None)
    tws_parser.add_argument("--hours", type=int, default=24, help="recency window for active tennis markets")
    tws_parser.add_argument("--dry-run", action="store_true", help="report only; persist nothing")
    tws_parser.add_argument("--scheduled", action="store_true", help="scheduled entry point (no-ops unless ENABLE_TENNIS_TICK_WATCHER=true)")
    twr_parser = subparsers.add_parser(
        "tennis-watch-report",
        help="TENNIS-WATCHER-001: tennis tick-coverage report (DB-only, read-only; never advice)",
    )
    twr_parser.add_argument("--hours", type=int, default=24)
    ttc_parser = subparsers.add_parser(
        "tennis-tape-capture-once",
        help="TENNIS-TAPE-001: one bounded synchronized score+market tape capture (read-only measurement; never advice)",
    )
    ttc_parser.add_argument("--limit", type=int, default=None)
    ttc_parser.add_argument("--hours", type=int, default=24, help="recency window for live tennis candidates")
    ttc_parser.add_argument("--dry-run", action="store_true", help="compute and report; persist nothing")
    tts_parser = subparsers.add_parser(
        "tennis-tape-capture-session",
        help="TENNIS-CAPTURE-SESSION-001: bounded repeated tape captures in one invocation (max 60 min; read-only measurement; never advice)",
    )
    tts_parser.add_argument("--duration-min", type=int, default=15)
    tts_parser.add_argument("--interval-sec", type=int, default=90)
    tts_parser.add_argument("--limit", type=int, default=None)
    tts_parser.add_argument("--dry-run", action="store_true")
    ttr_parser = subparsers.add_parser(
        "tennis-tape-report",
        help="TENNIS-TAPE-001: tape runs/links/freshness report (DB-only; never advice)",
    )
    ttr_parser.add_argument("--hours", type=int, default=24)
    ttr_parser.add_argument("--top", type=int, default=5)
    tlf_parser = subparsers.add_parser(
        "tennis-api-livefeed-probe",
        help="TENNIS-LIVE-FEED-002: bounded WebSocket live-feed validation vs Kalshi candidates (read-only; never advice)",
    )
    tlf_parser.add_argument("--duration-sec", type=int, default=60)
    tlf_parser.add_argument("--top", type=int, default=10)
    tgs_parser = subparsers.add_parser(
        "tennis-goalserve-probe",
        help="TENNIS-GOALSERVE-001: bounded Goalserve live-state validation vs Kalshi candidates (read-only; never advice)",
    )
    tgs_parser.add_argument("--probes", type=int, default=2)
    tgs_parser.add_argument("--interval-sec", type=int, default=20)
    tgs_parser.add_argument("--top", type=int, default=10)
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
    backup_parser = subparsers.add_parser(
        "backup-db", help="Compressed timestamped SQLite backup (+retention pruning)"
    )
    backup_parser.add_argument(
        "--dry-run", action="store_true",
        help="report capacity and retention plan; create and delete nothing",
    )
    backup_parser.add_argument(
        "--confirm", action="store_true",
        help="explicit confirmation (default behaviour; kept for symmetry)",
    )
    subparsers.add_parser("list-db-backups", help="List existing DB backups")
    verify_backup_parser = subparsers.add_parser(
        "verify-db-backup", help="Verify a backup is readable and complete"
    )
    verify_backup_parser.add_argument("path", type=str)
    prune_backups_parser = subparsers.add_parser(
        "prune-db-backups",
        help="Apply bounded tiered backup retention (needs --confirm to delete)",
    )
    prune_backups_parser.add_argument(
        "--dry-run", action="store_true", help="list retained/deleted/skipped only"
    )
    prune_backups_parser.add_argument(
        "--confirm", action="store_true", help="actually delete the planned backups"
    )
    freshness_parser = subparsers.add_parser(
        "sqlite-backup-freshness-report",
        help="Local manifest-backed backup health "
             "(SQLITE-BACKUP-FRESHNESS-ALERT-001; zero calls, no writes, "
             "runs no backup, prunes nothing)",
    )
    freshness_parser.add_argument(
        "--format", choices=("text", "json"), default="text", dest="fmt"
    )
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
    if args.command == "forecast-scorability-audit-report":
        return asyncio.run(
            forecast_scorability_audit_report(
                hours=args.hours, since=args.since, until=args.until,
                domain=args.domain, forecaster=args.forecaster, top=args.top, fmt=args.fmt,
            )
        )
    if args.command == "experiment-registry-validate":
        return experiment_registry_validate(manifest=args.manifest, fmt=args.fmt)
    if args.command == "experiment-registry-register":
        return experiment_registry_register(
            manifest=args.manifest, confirm=args.confirm and not args.dry_run,
            base=args.base, fmt=args.fmt)
    if args.command == "kalshi-realtime-replay":
        return kalshi_realtime_replay(
            archive=args.archive, environment=args.environment, fmt=args.fmt)
    if args.command == "archive-init":
        return archive_init(
            root=args.root, environment=args.environment,
            archive_identity=args.archive_identity,
            confirm=args.confirm and not args.dry_run, fmt=args.fmt)
    if args.command == "archive-recover-head":
        return archive_recover_head(
            root=args.root, environment=args.environment,
            confirm=args.confirm and not args.dry_run, fmt=args.fmt)
    if args.command == "archive-adopt":
        return archive_adopt(
            root=args.root, segment_id=args.segment_id,
            environment=args.environment,
            confirm=args.confirm and not args.dry_run, fmt=args.fmt)
    if args.command == "archive-migrate-legacy":
        return archive_migrate_legacy(
            source=args.source, dest=args.dest, environment=args.environment,
            archive_identity=args.archive_identity,
            confirm=args.confirm and not args.dry_run, fmt=args.fmt)
    if args.command == "experiment-registry-record-result":
        return experiment_registry_record_result(
            experiment_id=args.experiment_id,
            confirm=args.confirm and not args.dry_run, base=args.base,
            notes=args.notes, reevaluation_reason=args.reevaluation_reason,
            fmt=args.fmt)
    if args.command == "experiment-registry-report":
        return experiment_registry_report(
            experiment_id=args.experiment_id, base=args.base, fmt=args.fmt)
    if args.command == "experiment-registry-status":
        return experiment_registry_status(
            experiment_id=args.experiment_id, base=args.base, fmt=args.fmt)
    if args.command == "experiment-registry-list":
        return experiment_registry_list(base=args.base, fmt=args.fmt)
    if args.command == "experiment-registry-transition":
        return experiment_registry_transition(
            experiment_id=args.experiment_id, to=args.to,
            confirm=args.confirm and not args.dry_run, base=args.base,
            note=args.note, fmt=args.fmt)
    if args.command == "outcome-sync-coverage-report":
        return outcome_sync_coverage_report(
            hours=args.hours, since=args.since, until=args.until,
            domain=args.domain, examples=args.examples, fmt=args.fmt,
        )
    if args.command == "outcome-sync-backfill":
        # --dry-run always wins, so the pair is safe rather than destructive.
        return outcome_sync_backfill(
            confirm=args.confirm and not args.dry_run,
            max_markets=args.max_markets, domain=args.domain, fmt=args.fmt,
        )
    if args.command == "forecast-reliability-decomposition-report":
        return asyncio.run(
            forecast_reliability_decomposition_report(
                hours=args.hours, since=args.since, until=args.until,
                domain=args.domain, forecaster=args.forecaster, bins=args.bins,
                minimum_bin_count=args.minimum_bin_count,
                minimum_cohort_count=args.minimum_cohort_count, top=args.top, fmt=args.fmt,
            )
        )
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
    if args.command == "raw-payload-reclaim":
        # --dry-run always wins, so the pair is safe rather than destructive.
        outcome = raw_payload_reclaim(
            target=args.target, confirm=args.confirm and not args.dry_run,
            max_rows=args.max_rows, fmt=args.fmt,
        )
        # A run that was BLOCKED or failed verification must not look like
        # success to a wrapper, an && chain or a runbook. This is the one
        # irreversible command in the repo; silent zero is the wrong default.
        if outcome.get("mode") == "confirmed":
            clean = outcome.get("verified", False) and outcome.get(
                "stop_reason") in ("completed", "max_rows", "max_batches",
                                   "max_batch_seconds", "max_total_seconds")
            return 0 if clean else 1
        if outcome.get("refused"):
            return 2
        return 0 if outcome.get("backup_ok", True) else 1
    if args.command == "raw-payload-reclamation-report":
        raw_payload_reclamation_report(fmt=args.fmt)
        return 0
    if args.command == "raw-payload-storage-report":
        raw_payload_storage_report(
            fmt=args.fmt, recent=args.recent, full_scan=args.full_scan
        )
        return 0
    if args.command == "retention-coverage-report":
        retention_coverage_report(
            fmt=args.fmt, top=args.top, no_dbstat=args.no_dbstat
        )
        return 0
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
    if args.command == "db-schema-report":
        return asyncio.run(db_schema_report())
    if args.command == "db-integrity-check":
        return asyncio.run(db_integrity_check())
    if args.command == "db-growth-report":
        total = asyncio.run(db_growth_report(top=args.top))
        return 0 if total >= 0 else 1
    if args.command == "aggregate-market-ticks":
        n = asyncio.run(aggregate_market_ticks(
            hours=args.hours, bucket_seconds=args.bucket_seconds,
            dry_run=args.dry_run, max_rows=args.max_rows,
            subwindow_hours=args.subwindow_hours, scheduled=args.scheduled,
        ))
        return 0 if n >= 0 else 1
    if args.command == "tick-aggregation-report":
        n = asyncio.run(tick_aggregation_report())
        return 0 if n >= 0 else 1
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
        return asyncio.run(crypto_scan_once(
            limit=args.limit,
            provider_plan=args.provider_plan,
            allow_provider=args.allow_provider,
            deny_provider=args.deny_provider,
            confirm_paid_provider=args.confirm_paid_provider,
            yes=args.yes,
        ))
    if args.command == "crypto-signals-recent":
        count = asyncio.run(crypto_signals_recent(limit=args.limit))
        return 0 if count >= 0 else 1
    if args.command == "crypto-report":
        total = asyncio.run(crypto_report())
        return 0 if total >= 0 else 1
    if args.command == "crypto-risk-assess":
        count = asyncio.run(crypto_risk_assess(
            limit=args.limit,
            provider_plan=args.provider_plan,
            allow_provider=args.allow_provider,
            deny_provider=args.deny_provider,
            confirm_paid_provider=args.confirm_paid_provider,
            yes=args.yes,
        ))
        return 0 if count >= 0 else 1
    if args.command == "crypto-provider-health-report":
        return asyncio.run(crypto_provider_health_report())
    if args.command == "crypto-provider-budget-report":
        return asyncio.run(crypto_provider_budget_report())
    if args.command == "crypto-tape-run-once":
        n = asyncio.run(
            crypto_tape_run_once(
                limit=args.limit, hours=args.hours, dry_run=args.dry_run,
                source_crypto_run_id=args.source_crypto_run_id,
                confirm=args.confirm,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-tape-reconcile":
        n = asyncio.run(
            crypto_tape_reconcile(
                dry_run=args.dry_run, force=args.force,
                hours=args.hours, limit=args.limit,
                batch_size=args.batch_size,
                max_duration_seconds=args.max_duration_seconds,
                time_budget_seconds=args.time_budget_seconds,
                initial_per_token_cost_seconds=args.initial_per_token_cost_seconds,
                lock_wait_budget_seconds=args.lock_wait_budget_seconds,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-reconciler-health":
        n = asyncio.run(
            crypto_reconciler_health(
                clear=args.clear, operator=args.operator, note=args.note,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-tape-report":
        n = asyncio.run(crypto_tape_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "crypto-tape-session":
        n = asyncio.run(
            crypto_tape_session(
                duration_hours=args.duration_hours, interval_min=args.interval_min,
                limit=args.limit, dry_run=args.dry_run,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-horizon-cohort-create":
        n = asyncio.run(
            crypto_horizon_cohort_create(
                limit=args.limit, hours=args.hours, dry_run=args.dry_run,
                require_complete=args.require_complete,
                min_liquidity=args.min_liquidity,
                tokens=args.token, confirm=args.confirm,
                require_shared_horizon_windows=args.require_shared_horizon_windows,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-horizon-observe-once":
        n = asyncio.run(
            crypto_horizon_observe_once(
                cohort_id=args.cohort_id, limit=args.limit, dry_run=args.dry_run
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-horizon-schedule-report":
        asyncio.run(
            crypto_horizon_schedule_report(cohort_id=args.cohort_id, top=args.top)
        )
        return 0
    if args.command == "crypto-horizon-reminder-plan":
        asyncio.run(crypto_horizon_reminder_plan(cohort_id=args.cohort_id))
        return 0
    if args.command == "crypto-horizon-arm-cohort":
        result = asyncio.run(
            crypto_horizon_arm_cohort(
                cohort_id=args.cohort_id,
                dry_run=args.dry_run,
                confirm=args.confirm,
            )
        )
        return 0 if result["status"] in {"ok", "armed"} else 1
    if args.command == "crypto-horizon-run-job":
        return asyncio.run(
            crypto_horizon_run_job(
                cohort_id=args.cohort_id, job_id=args.job_id
            )
        )
    if args.command == "crypto-horizon-orchestrator-report":
        asyncio.run(crypto_horizon_orchestrator_report(cohort_id=args.cohort_id))
        return 0
    if args.command == "crypto-horizon-disarm-cohort":
        result = asyncio.run(
            crypto_horizon_disarm_cohort(
                cohort_id=args.cohort_id, confirm=args.confirm
            )
        )
        return 0 if result["status"] in {"disarmed", "already_disarmed"} else 1
    if args.command == "crypto-horizon-shared-candidate-feasibility-report":
        asyncio.run(
            crypto_horizon_shared_candidate_feasibility_report(
                hours=args.hours, days=args.days, limit=args.limit,
                fmt=args.fmt, arm_margin_seconds=args.arm_margin_seconds,
            )
        )
        return 0
    if args.command == "crypto-horizon-candidate-readiness-report":
        asyncio.run(
            crypto_horizon_candidate_readiness_report(
                at=args.at, limit=args.limit, fmt=args.fmt,
                require_complete=args.require_complete,
                minimum_arm_margin_seconds=args.minimum_arm_margin_seconds,
            )
        )
        return 0
    if args.command == "crypto-horizon-candidate-readiness-history-report":
        asyncio.run(
            crypto_horizon_candidate_readiness_history_report(
                limit=args.limit, fmt=args.fmt,
            )
        )
        return 0
    if args.command == "crypto-horizon-observation-report":
        n = asyncio.run(
            crypto_horizon_observation_report(
                cohort_id=args.cohort_id, top=args.top, shadow=args.shadow
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-horizon-pair-selection-report":
        n = asyncio.run(
            crypto_horizon_pair_selection_report(cohort_id=args.cohort_id, top=args.top)
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-horizon-outcome-reconciliation-report":
        n = asyncio.run(
            crypto_horizon_outcome_reconciliation_report(
                cohort_id=args.cohort_id, top=args.top
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-tape-coverage-report":
        n = asyncio.run(
            crypto_tape_coverage_report(hours=args.hours, top=args.top, limit=args.limit)
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-sparse-observe":
        n = asyncio.run(
            crypto_sparse_observe(
                dry_run=args.dry_run, force=args.force,
                enrol_limit=args.enrol_limit,
                observe_limit=args.observe_limit,
                write_batch_size=args.write_batch_size,
                max_duration_seconds=args.max_duration_seconds,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-observation-coverage-report":
        # B6. Explicit `--hours` always wins; `--all` is the only way to the
        # unbounded scan; neither given means the bounded default.
        from app.services.crypto_sparse_observation import (
            DEFAULT_REPORT_HOURS as OBS_COVERAGE_DEFAULT_HOURS,
        )

        if args.all_history and args.hours is not None:
            print("crypto-observation-coverage-report: --hours and --all conflict")
            return 1
        hours = None if args.all_history else (
            args.hours if args.hours is not None else OBS_COVERAGE_DEFAULT_HOURS
        )
        n = asyncio.run(
            crypto_observation_coverage_report(hours=hours, top=args.top)
        )
        return 0 if n >= 0 else 1
    if args.command == "crypto-retrospect-report":
        n = asyncio.run(
            crypto_retrospect_report(hours=args.hours, top=args.top, cohort=args.cohort)
        )
        return 0 if n >= 0 else 1
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
    if args.command == "marketops-reconcile-db-growth-alerts":
        # --confirm is the ONLY mutating path; --dry-run is the default and is
        # also what you get when neither flag is passed.
        marketops_reconcile_db_growth_alerts(
            confirm=args.confirm and not args.dry_run, fmt=args.fmt
        )
        return 0
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
    if args.command == "edge-followthrough-diagnostic-report":
        n = asyncio.run(edge_followthrough_diagnostic_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "edge-filter-shadow-report":
        n = asyncio.run(edge_filter_shadow_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "forecast-anchor-diagnostic-report":
        n = asyncio.run(forecast_anchor_diagnostic_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "trigger-timing-shadow-report":
        n = asyncio.run(trigger_timing_shadow_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "edge-selection-validation-report":
        n = asyncio.run(
            edge_selection_validation_report(
                hours=args.hours, since=args.since, until=args.until
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "edge-selection-retirement-report":
        n = asyncio.run(edge_selection_retirement_report())
        return 0 if n >= 0 else 1
    if args.command == "edge-cost-shadow-report":
        n = asyncio.run(edge_cost_shadow_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "live-market-state-report":
        n = asyncio.run(
            live_market_state_report(domain=args.domain, top=args.top, hours=args.hours)
        )
        return 0 if n >= 0 else 1
    if args.command == "tennis-live-source-report":
        n = asyncio.run(tennis_live_source_report(top=args.top, hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "tennis-watch-scan-once":
        n = asyncio.run(
            tennis_watch_scan_once(
                limit=args.limit, hours=args.hours,
                dry_run=args.dry_run, scheduled=args.scheduled,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "tennis-watch-report":
        n = asyncio.run(tennis_watch_report(hours=args.hours))
        return 0 if n >= 0 else 1
    if args.command == "tennis-tape-capture-once":
        n = asyncio.run(
            tennis_tape_capture_once(
                limit=args.limit, hours=args.hours, dry_run=args.dry_run
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "tennis-tape-capture-session":
        n = asyncio.run(
            tennis_tape_capture_session(
                duration_min=args.duration_min, interval_sec=args.interval_sec,
                limit=args.limit, dry_run=args.dry_run,
            )
        )
        return 0 if n >= 0 else 1
    if args.command == "tennis-tape-report":
        n = asyncio.run(tennis_tape_report(hours=args.hours, top=args.top))
        return 0 if n >= 0 else 1
    if args.command == "tennis-api-livefeed-probe":
        n = asyncio.run(
            tennis_api_livefeed_probe(duration_sec=args.duration_sec, top=args.top)
        )
        return 0 if n >= 0 else 1
    if args.command == "tennis-goalserve-probe":
        n = asyncio.run(
            tennis_goalserve_probe(
                probes=args.probes, interval_sec=args.interval_sec, top=args.top
            )
        )
        return 0 if n >= 0 else 1
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
        return asyncio.run(backup_db(dry_run=args.dry_run))
    if args.command == "list-db-backups":
        count = asyncio.run(list_db_backups())
        return 0 if count >= 0 else 1
    if args.command == "verify-db-backup":
        return asyncio.run(verify_db_backup(path=args.path))
    if args.command == "prune-db-backups":
        return asyncio.run(prune_db_backups(confirm=args.confirm))
    if args.command == "sqlite-backup-freshness-report":
        # 0 = healthy, 1 = unhealthy — same convention as verify-db-backup, so a
        # report that exits 0 always means backup protection is actually there.
        return 0 if sqlite_backup_freshness_report(fmt=args.fmt)["healthy"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
