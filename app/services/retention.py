"""Retention/pruning for high-churn OPERATIONAL tables:

- market_price_ticks       (watcher quote snapshots — the growth driver)
- watcher_runs             (per-pass audit rows)
- crypto_price_ticks       (crypto scout snapshots, CRYPTO-001)
- crypto_watcher_runs      (crypto scan audit rows, CRYPTO-001)
- pipeline_runs / pipeline_stage_runs (baseline audit rows)
- opportunity_signals      (ONLY when SIGNAL_RETENTION_DAYS > 0; default keeps
                            them indefinitely)

Intelligence and calibration tables are NEVER touched by this service:
markets, market_snapshots, scanner_runs, eligibility assessments,
enrichments, resolution assessments, research packets, forecasts,
market_outcomes, forecast_scores. See PROTECTED_TABLES.

Deletes run in batches to keep transactions short; dry-run counts without
deleting anything. Read-only project posture is unchanged — this deletes
only our own audit/telemetry rows, never anything external."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoPriceTick,
    CryptoWatcherRun,
    MarketPriceTick,
    OpportunitySignal,
    PipelineRun,
    PipelineStageRun,
    WatcherRun,
)

logger = logging.getLogger(__name__)

# Documentation + test contract: these tables must never be pruned.
PROTECTED_TABLES = (
    "markets",
    "market_snapshots",
    "orderbook_snapshots",
    "scanner_runs",
    "market_eligibility_assessments",
    "market_detail_enrichments",
    "market_resolution_assessments",
    "market_research_packets",
    "market_forecasts",
    "market_outcomes",
    "forecast_scores",
    # Crypto Arena (CRYPTO-001): tokens/pairs/events/risk/signals are audit
    # history — only crypto ticks and run rows are ever pruned.
    "crypto_tokens",
    "crypto_pairs",
    "crypto_token_discovery_events",
    "crypto_token_risk_assessments",
    "crypto_opportunity_signals",
)


@dataclass
class RetentionConfig:
    tick_days: int = 7
    watcher_run_days: int = 30
    pipeline_run_days: int = 90
    signal_days: int = 0  # 0 = keep forever
    crypto_days: int = 7  # crypto_price_ticks + crypto_watcher_runs
    batch_size: int = 5000

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **overrides) -> "RetentionConfig":
        s = settings or get_settings()
        values = {
            "tick_days": s.tick_retention_days,
            "watcher_run_days": s.watcher_run_retention_days,
            "pipeline_run_days": s.pipeline_run_retention_days,
            "signal_days": s.signal_retention_days,
            "crypto_days": s.crypto_retention_days,
            "batch_size": s.retention_batch_size,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


class RetentionService:
    def __init__(self, config: RetentionConfig | None = None):
        self.config = config or RetentionConfig.from_settings()

    def _delete_batched(self, session: Session, model, condition, dry_run: bool) -> int:
        if dry_run:
            return session.execute(
                select(func.count()).select_from(model).where(condition)
            ).scalar() or 0
        total = 0
        while True:
            ids = session.execute(
                select(model.id).where(condition).limit(self.config.batch_size)
            ).scalars().all()
            if not ids:
                break
            session.execute(delete(model).where(model.id.in_(ids)))
            session.commit()
            total += len(ids)
        return total

    def prune(self, session: Session, dry_run: bool = False) -> dict[str, int]:
        """Prune per configured retention windows. Returns rows deleted per
        table (rows that WOULD be deleted when dry_run=True)."""
        cfg = self.config
        counts: dict[str, int] = {}

        counts["market_price_ticks"] = self._delete_batched(
            session, MarketPriceTick, MarketPriceTick.created_at < _cutoff(cfg.tick_days), dry_run
        )
        counts["watcher_runs"] = self._delete_batched(
            session, WatcherRun, WatcherRun.created_at < _cutoff(cfg.watcher_run_days), dry_run
        )
        counts["crypto_price_ticks"] = self._delete_batched(
            session, CryptoPriceTick, CryptoPriceTick.created_at < _cutoff(cfg.crypto_days), dry_run
        )
        counts["crypto_watcher_runs"] = self._delete_batched(
            session,
            CryptoWatcherRun,
            (CryptoWatcherRun.created_at < _cutoff(cfg.crypto_days))
            & (CryptoWatcherRun.status != "running"),
            dry_run,
        )

        # Pipeline: stage rows first (children), then their parent runs.
        # A row still marked 'running' is never pruned regardless of age.
        pipeline_cutoff = _cutoff(cfg.pipeline_run_days)
        old_run_ids = select(PipelineRun.id).where(
            PipelineRun.created_at < pipeline_cutoff, PipelineRun.status != "running"
        ).scalar_subquery()
        counts["pipeline_stage_runs"] = self._delete_batched(
            session, PipelineStageRun, PipelineStageRun.pipeline_run_id.in_(old_run_ids), dry_run
        )
        counts["pipeline_runs"] = self._delete_batched(
            session,
            PipelineRun,
            (PipelineRun.created_at < pipeline_cutoff) & (PipelineRun.status != "running"),
            dry_run,
        )

        if cfg.signal_days > 0:
            counts["opportunity_signals"] = self._delete_batched(
                session,
                OpportunitySignal,
                OpportunitySignal.created_at < _cutoff(cfg.signal_days),
                dry_run,
            )
        else:
            counts["opportunity_signals"] = 0  # retention disabled: keep forever

        if not dry_run and any(counts.values()):
            logger.info("Retention pruned: %s", counts)
        return counts
