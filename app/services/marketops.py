"""MarketOps Autopilot (OPS-006): 24/7 read-only coordination of the existing
market agents (Kalshi signal workflow, baseball/soccer research canaries,
crypto scout, outcome sync, calibration, champion/challenger).

One cycle = inspect signals -> auto-promote top-N -> process promoted ->
crypto scan -> sync outcomes -> score forecasts -> champion/challenger
snapshot -> local DB alerts -> one marketops_runs audit row. Every stage is
individually guarded: a failing stage records its error in the run summary
(and a provider_error alert) and the cycle continues unless fail_fast is set.

This layer creates NO new market capability — it only sequences existing
read-only services. No EV calculation, no paper trading, no trade
recommendations, no portfolio sizing, no order placement, no wallets/keys,
no swaps/transaction signing, no autonomous trading. Alerts are local DB
rows (no external Slack/Discord delivery in OPS-006). See
docs/SAFETY_BOUNDARIES.md.
"""

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoOpportunitySignal,
    CryptoToken,
    MarketOpsAlert,
    MarketOpsRun,
    OpportunitySignal,
    WatcherRun,
)
from app.services.signal_workflow import (
    PROMOTION_PRIORITY,
    STATUS_FORECAST_REFRESHED,
    STATUS_NEW,
    STATUS_PROMOTED,
    SignalProcessingService,
    SignalPromotionService,
)

logger = logging.getLogger(__name__)

# Alert types (local DB only; no external delivery in OPS-006)
ALERT_SERVICE_HEALTH = "service_health_warning"
ALERT_TOO_MANY_SIGNALS = "too_many_signals"
ALERT_NO_RECENT_SIGNALS = "no_recent_signals"
ALERT_CRYPTO_SPIKE = "crypto_signal_spike"
ALERT_SOURCE_BACKED_FORECAST = "source_backed_forecast_created"
ALERT_CC_SAMPLE_UPDATE = "champion_challenger_sample_update"
ALERT_PROVIDER_ERROR = "provider_error"
ALERT_DB_GROWTH = "db_growth_warning"

ALERT_STATUS_OPEN = "open"
ALERT_STATUS_RESOLVED = "resolved"

# Deterministic thresholds (shape of each rule; operational limits live in config)
TOO_MANY_SIGNALS_PER_HOUR = 150
NO_SIGNAL_WINDOW_HOURS = 6
WATCHER_STALE_MINUTES = 30
CRYPTO_SIGNAL_SPIKE_PER_CYCLE = 25
DB_GROWTH_WARNING_MB = 512.0
TICKER_REFRESH_COOLDOWN_SECONDS = 3600  # don't re-promote a just-refreshed ticker

# Domains whose promoted signals can currently become source-backed packets
SOURCE_BACKED_CAPABLE_DOMAINS = ("sports_baseball", "sports_soccer")


@dataclass
class MarketOpsConfig:
    promote_limit: int = 5
    process_limit: int = 5
    crypto_scan_limit: int = 100
    sync_outcome_limit: int = 500
    score_limit: int = 1000
    min_signal_age_seconds: int = 30
    max_signal_age_hours: int = 24
    include_crypto: bool = True
    include_probability_markets: bool = True
    fail_fast: bool = False

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "MarketOpsConfig":
        s = settings or get_settings()
        return cls(
            promote_limit=s.marketops_promote_limit,
            process_limit=s.marketops_process_limit,
            crypto_scan_limit=s.marketops_crypto_scan_limit,
            sync_outcome_limit=s.marketops_sync_outcome_limit,
            score_limit=s.marketops_score_limit,
            min_signal_age_seconds=s.marketops_min_signal_age_seconds,
            max_signal_age_hours=s.marketops_max_signal_age_hours,
            include_crypto=s.marketops_include_crypto,
            include_probability_markets=s.marketops_include_probability_markets,
            fail_fast=s.marketops_fail_fast,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ticker_domain(ticker: str) -> str:
    """Deterministic domain from ticker prefix markers only (title/category
    are not loaded here; prefix rules cover every source-backed domain)."""
    from app.services.research import DOMAIN_GENERAL, DOMAIN_RULES

    upper = ticker.upper()
    for domain, markers, _keywords in DOMAIN_RULES:
        if any(upper.startswith(marker) for marker in markers):
            return domain
    return DOMAIN_GENERAL


def _priority_index(signal_type: str) -> int:
    try:
        return PROMOTION_PRIORITY.index(signal_type)
    except ValueError:
        return len(PROMOTION_PRIORITY)


class MarketOpsAlertService:
    """Local DB alerts with open-duplicate suppression."""

    def create(
        self,
        session: Session,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        evidence: dict | None = None,
    ) -> MarketOpsAlert | None:
        """Create an alert unless an identical (type, title) alert is already
        open — repeated cycles must not stack duplicates."""
        existing = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.title == title,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            )
        ).scalars().first()
        if existing is not None:
            return None
        alert = MarketOpsAlert(
            alert_type=alert_type,
            severity=severity,
            status=ALERT_STATUS_OPEN,
            title=title,
            message=message,
            evidence=evidence,
            created_at=_now(),
        )
        session.add(alert)
        session.flush()
        return alert

    def resolve(self, session: Session, alert_id: int) -> MarketOpsAlert:
        alert = session.get(MarketOpsAlert, alert_id)
        if alert is None:
            raise LookupError(f"Alert {alert_id} not found")
        if alert.status != ALERT_STATUS_RESOLVED:
            alert.status = ALERT_STATUS_RESOLVED
            alert.resolved_at = _now()
            session.commit()
        return alert

    def list_recent(
        self, session: Session, limit: int = 20, status: str | None = None
    ) -> list[MarketOpsAlert]:
        query = select(MarketOpsAlert).order_by(MarketOpsAlert.id.desc()).limit(limit)
        if status:
            query = query.where(MarketOpsAlert.status == status)
        return list(session.execute(query).scalars().all())


class MarketOpsAutopilotService:
    """One coordination cycle over existing read-only services. All
    collaborators are injectable for tests; defaults follow env flags."""

    def __init__(
        self,
        config: MarketOpsConfig | None = None,
        promotion_service: SignalPromotionService | None = None,
        processing_service: SignalProcessingService | None = None,
        crypto_service=None,
        outcome_service=None,
        calibration_service=None,
        champion_challenger_service=None,
        alert_service: MarketOpsAlertService | None = None,
    ):
        self.config = config or MarketOpsConfig.from_settings()
        self.promotion_service = promotion_service or SignalPromotionService()
        self.processing_service = processing_service or SignalProcessingService()
        self._crypto_service = crypto_service
        self._outcome_service = outcome_service
        self._calibration_service = calibration_service
        self._cc_service = champion_challenger_service
        self.alert_service = alert_service or MarketOpsAlertService()

    # --- stage helpers -----------------------------------------------------

    def _eligible_signals(self, session: Session, now: datetime) -> list[OpportunitySignal]:
        """Fresh 'new' signals inside the [min_age, max_age] window. Dismissed/
        reviewed/errored signals are excluded by the status filter."""
        cfg = self.config
        newest = now - timedelta(seconds=cfg.min_signal_age_seconds)
        oldest = now - timedelta(hours=cfg.max_signal_age_hours)
        rows = session.execute(
            select(OpportunitySignal).where(
                OpportunitySignal.signal_status == STATUS_NEW,
                OpportunitySignal.processing_error_type.is_(None),
                OpportunitySignal.observed_at <= newest,
                OpportunitySignal.observed_at >= oldest,
            )
        ).scalars().all()
        return list(rows)

    def _recently_refreshed_tickers(self, session: Session, now: datetime) -> set[str]:
        cutoff = now - timedelta(seconds=TICKER_REFRESH_COOLDOWN_SECONDS)
        rows = session.execute(
            select(OpportunitySignal.market_ticker).where(
                OpportunitySignal.signal_status == STATUS_FORECAST_REFRESHED,
                OpportunitySignal.processed_at.is_not(None),
                OpportunitySignal.processed_at >= cutoff,
            )
        ).scalars().all()
        return set(rows)

    def _tickers_awaiting_processing(self, session: Session) -> set[str]:
        rows = session.execute(
            select(OpportunitySignal.market_ticker).where(
                OpportunitySignal.signal_status == STATUS_PROMOTED
            )
        ).scalars().all()
        return set(rows)

    def select_signals_for_promotion(
        self, session: Session, now: datetime | None = None
    ) -> tuple[list[OpportunitySignal], int]:
        """Deterministic auto-promotion selection: source-backed-capable
        domains (baseball/soccer) first, then signal-type priority, then
        newest; at most one signal per ticker per cycle; tickers refreshed
        within the last hour or already awaiting processing are skipped.
        Returns (selected, total_seen)."""
        now = now or _now()
        candidates = self._eligible_signals(session, now)
        seen = len(candidates)
        skip_tickers = self._recently_refreshed_tickers(session, now)
        skip_tickers |= self._tickers_awaiting_processing(session)

        candidates.sort(
            key=lambda s: (
                0 if _ticker_domain(s.market_ticker) in SOURCE_BACKED_CAPABLE_DOMAINS else 1,
                _priority_index(s.signal_type),
                -s.id,
            )
        )
        selected: list[OpportunitySignal] = []
        used_tickers: set[str] = set()
        for signal in candidates:
            if len(selected) >= self.config.promote_limit:
                break
            if signal.market_ticker in skip_tickers or signal.market_ticker in used_tickers:
                continue
            used_tickers.add(signal.market_ticker)
            selected.append(signal)
        return selected, seen

    # --- lazily-built default collaborators --------------------------------

    @property
    def crypto_service(self):
        if self._crypto_service is None:
            from app.services.crypto_scout import CryptoDiscoveryService

            self._crypto_service = CryptoDiscoveryService()
        return self._crypto_service

    @property
    def outcome_service(self):
        if self._outcome_service is None:
            from app.services.outcomes import OutcomeService

            self._outcome_service = OutcomeService()
        return self._outcome_service

    @property
    def calibration_service(self):
        if self._calibration_service is None:
            from app.services.calibration import CalibrationService

            self._calibration_service = CalibrationService()
        return self._calibration_service

    @property
    def cc_service(self):
        if self._cc_service is None:
            from app.services.champion_challenger import ChampionChallengerService

            self._cc_service = ChampionChallengerService()
        return self._cc_service

    # --- the cycle ----------------------------------------------------------

    async def run_once(self, session: Session) -> MarketOpsRun:
        """One autopilot cycle. Stage failures are captured per stage in the
        run summary and the cycle continues (unless fail_fast); only setup
        failures mark the whole run as error."""
        cfg = self.config
        started_at = _now()
        run = MarketOpsRun(
            status="running",
            started_at=started_at,
            config=asdict(cfg),
            created_at=started_at,
        )
        session.add(run)
        session.commit()

        summary: dict = {"stages": {}, "stage_errors": {}}
        alerts_created = 0

        async def stage(name: str, coro_factory):
            nonlocal alerts_created
            try:
                result = await coro_factory()
                summary["stages"][name] = "ok"
                return result
            except Exception as exc:
                logger.exception("MarketOps stage %r failed", name)
                summary["stages"][name] = "error"
                summary["stage_errors"][name] = f"{type(exc).__name__}: {str(exc)[:500]}"
                alert = self.alert_service.create(
                    session,
                    ALERT_PROVIDER_ERROR,
                    "warning",
                    f"MarketOps stage failed: {name}",
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                    evidence={"stage": name, "run_id": run.id},
                )
                if alert is not None:
                    alerts_created += 1
                if cfg.fail_fast:
                    raise
                return None

        try:
            now = _now()

            # 1-3. Probability-market lane: inspect -> promote -> process
            if cfg.include_probability_markets:

                async def promote():
                    selected, seen = self.select_signals_for_promotion(session, now)
                    run.signals_seen = seen
                    promoted = [
                        self.promotion_service.promote(session, signal.id)
                        for signal in selected
                    ]
                    return promoted

                promoted = await stage("promote_signals", promote) or []
                run.signals_promoted = len(promoted)

                async def process():
                    return await self.processing_service.process_promoted(
                        session, limit=cfg.process_limit
                    )

                processed = await stage("process_promoted", process) or []
                run.signals_processed = len(processed)
                summary["processed_tickers"] = [s.market_ticker for s in processed]

                # Informational alert for each source-backed refresh this cycle
                from app.services.signal_workflow import refreshed_packet_summary

                for signal in processed:
                    packet_summary = refreshed_packet_summary(session, signal)
                    if packet_summary and packet_summary.evidence_depth == "source_backed":
                        alert = self.alert_service.create(
                            session,
                            ALERT_SOURCE_BACKED_FORECAST,
                            "info",
                            f"Source-backed refresh: {signal.market_ticker}",
                            f"Signal #{signal.id} refreshed with "
                            f"{packet_summary.collector_name} "
                            f"(completeness {packet_summary.research_completeness_score})",
                            evidence={
                                "signal_id": signal.id,
                                "packet_id": packet_summary.packet_id,
                                "collector": packet_summary.collector_name,
                            },
                        )
                        if alert is not None:
                            alerts_created += 1
            else:
                summary["stages"]["probability_markets"] = "skipped"

            # 4. Crypto lane
            if cfg.include_crypto:

                async def crypto():
                    return await self.crypto_service.scan_once(
                        session, limit=cfg.crypto_scan_limit
                    )

                crypto_run = await stage("crypto_scan", crypto)
                if crypto_run is not None:
                    run.crypto_tokens_seen = crypto_run.tokens_checked
                    run.crypto_signals_created = crypto_run.signals_created
                    if crypto_run.signals_created >= CRYPTO_SIGNAL_SPIKE_PER_CYCLE:
                        alert = self.alert_service.create(
                            session,
                            ALERT_CRYPTO_SPIKE,
                            "warning",
                            f"Crypto signal spike: {crypto_run.signals_created} in one scan",
                            f"Scan #{crypto_run.id} created {crypto_run.signals_created} "
                            f"signals (threshold {CRYPTO_SIGNAL_SPIKE_PER_CYCLE})",
                            evidence={"crypto_run_id": crypto_run.id},
                        )
                        if alert is not None:
                            alerts_created += 1
            else:
                summary["stages"]["crypto_scan"] = "skipped"

            # 5. Outcome sync + scoring (safe: read-only GETs + local scoring)
            async def sync():
                synced = await self.outcome_service.sync_known_markets(
                    session, limit=cfg.sync_outcome_limit
                )
                return len(synced)

            run.outcomes_synced = await stage("sync_outcomes", sync) or 0

            async def score():
                counts = self.calibration_service.score_unscored(
                    session, limit=cfg.score_limit
                )
                summary["score_counts"] = counts
                return counts["scored"]

            run.forecasts_scored = await stage("score_forecasts", score) or 0

            # 6. Champion/challenger snapshot (+ sample-update alert)
            async def compare():
                comparison = self.cc_service.compare(session)
                snapshot = {
                    "baseline": comparison.baseline_forecaster,
                    "challenger": comparison.challenger_forecaster,
                    "pair_count": comparison.paired.pair_count if comparison.paired else 0,
                    "sample_label": (
                        comparison.paired.sample_label
                        if comparison.paired
                        else comparison.sample_label
                    ),
                    "mean_delta_brier": (
                        comparison.paired.mean_delta_brier if comparison.paired else None
                    ),
                }
                summary["champion_challenger"] = snapshot
                return snapshot

            cc_snapshot = await stage("champion_challenger", compare)
            if cc_snapshot is not None:
                previous = session.execute(
                    select(MarketOpsRun)
                    .where(MarketOpsRun.id != run.id, MarketOpsRun.summary.is_not(None))
                    .order_by(MarketOpsRun.id.desc())
                ).scalars().first()
                previous_pairs = (
                    ((previous.summary or {}).get("champion_challenger") or {}).get(
                        "pair_count", 0
                    )
                    if previous
                    else 0
                )
                if cc_snapshot["pair_count"] != previous_pairs:
                    alert = self.alert_service.create(
                        session,
                        ALERT_CC_SAMPLE_UPDATE,
                        "info",
                        f"Champion/challenger pairs: {previous_pairs} -> "
                        f"{cc_snapshot['pair_count']}",
                        f"Paired sample now {cc_snapshot['pair_count']} "
                        f"({cc_snapshot['sample_label']}), "
                        f"mean_delta_brier={cc_snapshot['mean_delta_brier']}",
                        evidence=cc_snapshot,
                    )
                    if alert is not None:
                        alerts_created += 1

            # 7. Health / hygiene alerts
            alerts_created += self._health_alerts(session, now)

            run.alerts_created = alerts_created
            run.summary = summary
            run.status = (
                "ok" if not summary["stage_errors"] else "partial"
            )
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            return run
        except Exception as exc:
            session.rollback()
            logger.exception("MarketOps cycle failed")
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
            run.summary = summary
            run.alerts_created = alerts_created
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            return run

    def _health_alerts(self, session: Session, now: datetime) -> int:
        """Deterministic health checks -> local alerts. Returns alerts created."""
        created = 0
        settings = get_settings()

        hour_ago = now - timedelta(hours=1)
        signals_last_hour = session.execute(
            select(func.count()).select_from(OpportunitySignal).where(
                OpportunitySignal.created_at >= hour_ago
            )
        ).scalar() or 0
        if signals_last_hour > TOO_MANY_SIGNALS_PER_HOUR:
            if self.alert_service.create(
                session,
                ALERT_TOO_MANY_SIGNALS,
                "warning",
                f"Signal flood: {signals_last_hour} signals in the last hour",
                f"Threshold {TOO_MANY_SIGNALS_PER_HOUR}/h — check watcher thresholds/cooldowns",
                evidence={"signals_last_hour": signals_last_hour},
            ):
                created += 1

        window_start = now - timedelta(hours=NO_SIGNAL_WINDOW_HOURS)
        recent = session.execute(
            select(func.count()).select_from(OpportunitySignal).where(
                OpportunitySignal.created_at >= window_start
            )
        ).scalar() or 0
        if recent == 0 and settings.enable_realtime_watcher:
            if self.alert_service.create(
                session,
                ALERT_NO_RECENT_SIGNALS,
                "warning",
                f"No signals in {NO_SIGNAL_WINDOW_HOURS}h",
                "Watcher is enabled but produced no signals — verify the service and market hours",
                evidence={"window_hours": NO_SIGNAL_WINDOW_HOURS},
            ):
                created += 1

        if settings.enable_realtime_watcher:
            latest_watcher = session.execute(
                select(WatcherRun).order_by(WatcherRun.id.desc())
            ).scalars().first()
            stale_cutoff = now - timedelta(minutes=WATCHER_STALE_MINUTES)
            watcher_started = _aware(latest_watcher.started_at) if latest_watcher else None
            if (
                latest_watcher is None
                or watcher_started < stale_cutoff
                or latest_watcher.status == "error"
            ):
                detail = (
                    "no watcher runs recorded"
                    if latest_watcher is None
                    else f"latest run #{latest_watcher.id} status={latest_watcher.status} "
                    f"started={watcher_started}"
                )
                if self.alert_service.create(
                    session,
                    ALERT_SERVICE_HEALTH,
                    "warning",
                    "Watcher looks stale or errored",
                    f"{detail} (stale threshold {WATCHER_STALE_MINUTES}min)",
                    evidence={"stale_minutes": WATCHER_STALE_MINUTES},
                ):
                    created += 1

        size_mb = database_size_mb()
        if size_mb is not None and size_mb >= DB_GROWTH_WARNING_MB:
            if self.alert_service.create(
                session,
                ALERT_DB_GROWTH,
                "warning",
                f"Database at {size_mb:.0f} MiB",
                f"SQLite file exceeds {DB_GROWTH_WARNING_MB:.0f} MiB — review retention windows",
                evidence={"size_mb": round(size_mb, 2)},
            ):
                created += 1

        return created


def database_size_mb(settings: Settings | None = None) -> float | None:
    """Best-effort DB size (SQLite file only); None when unavailable."""
    import os

    from sqlalchemy.engine.url import make_url

    settings = settings or get_settings()
    try:
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite" and url.database and os.path.exists(url.database):
            return os.path.getsize(url.database) / (1024 * 1024)
    except Exception:  # pragma: no cover - defensive
        logger.debug("database_size_mb unavailable", exc_info=True)
    return None


class MarketOpsReportService:
    """Aggregate MarketOps view + a deterministic recommended operator action."""

    def build(self, session: Session, recent_limit: int = 10):
        from app.schemas import MarketOpsAlertOut, MarketOpsReport, MarketOpsRunOut
        from app.services.baseball_research import build_research_canary_report

        latest_run = session.execute(
            select(MarketOpsRun).order_by(MarketOpsRun.id.desc())
        ).scalars().first()
        runs_total = session.execute(
            select(func.count()).select_from(MarketOpsRun)
        ).scalar() or 0

        open_alerts = MarketOpsAlertService().list_recent(
            session, limit=recent_limit, status=ALERT_STATUS_OPEN
        )

        canary = build_research_canary_report(session)
        source_backed_packets = sum(
            stats.by_evidence_depth.get("source_backed", 0)
            for name, stats in canary.by_collector.items()
            if name.endswith("-external")
        )

        crypto_totals = {
            "tokens": session.execute(
                select(func.count()).select_from(CryptoToken)
            ).scalar() or 0,
            "signals": session.execute(
                select(func.count()).select_from(CryptoOpportunitySignal)
            ).scalar() or 0,
        }

        cc_snapshot = (
            (latest_run.summary or {}).get("champion_challenger") if latest_run else None
        )

        recommended = self._recommend(latest_run, open_alerts, cc_snapshot)

        return MarketOpsReport(
            runs_total=runs_total,
            latest_run=MarketOpsRunOut.model_validate(latest_run) if latest_run else None,
            open_alerts=[MarketOpsAlertOut.model_validate(a) for a in open_alerts],
            source_backed_packets=source_backed_packets,
            forecasts_by_forecaster=canary.forecasts_by_forecaster,
            champion_challenger=cc_snapshot,
            crypto_totals=crypto_totals,
            database_size_mb=(
                round(size, 2) if (size := database_size_mb()) is not None else None
            ),
            recommended_action=recommended,
        )

    @staticmethod
    def _recommend(latest_run, open_alerts, cc_snapshot) -> str:
        urgent = [a for a in open_alerts if a.severity in ("warning", "critical")]
        if urgent:
            return (
                f"Investigate {len(urgent)} open warning/critical alert(s) "
                "(marketops-alerts), then resolve them (marketops-resolve-alert <id>)"
            )
        if latest_run is None:
            return "Run `marketops-run-once` to record the first coordination cycle"
        if latest_run.status == "error":
            return f"Inspect last run error: {latest_run.error_type}"
        if latest_run.status == "partial":
            return "Review stage_errors in the last run summary"
        if cc_snapshot and cc_snapshot.get("sample_label") in (
            "insufficient_sample",
            "early_signal",
        ):
            return (
                "No action needed — keep accumulating paired outcomes before "
                "reading anything into champion/challenger deltas"
            )
        return "No action needed"
