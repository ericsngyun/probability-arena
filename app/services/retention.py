"""Retention/pruning for high-churn OPERATIONAL tables:

- market_price_ticks       (watcher quote snapshots — the growth driver)
- watcher_runs             (per-pass audit rows)
- crypto_price_ticks       (crypto scout snapshots, CRYPTO-001)
- crypto_watcher_runs      (crypto scan audit rows, CRYPTO-001)
- pipeline_runs / pipeline_stage_runs (baseline audit rows)
- opportunity_signals      (ONLY when SIGNAL_RETENTION_DAYS > 0; default keeps
                            them indefinitely)
- meme_scout_runs / meme_attention_snapshots / meme_catalyst_events
                           (MEME-NEWS-002 always-on lane, pruned after
                            MEME_NEWS_RETENTION_DAYS — DOCUMENTED: the report
                            and alerts operate on recent windows, so older
                            attention/catalyst rows are low-value; bounding
                            them keeps the scheduled lane's growth capped. The
                            domain-scout inventory tables are NOT pruned.)
- polymarket_markets / polymarket_orderbook_snapshots / polymarket_scout_runs
                           (POLY-001 always-on observer lane, pruned after
                            POLYMARKET_RETENTION_DAYS — DOCUMENTED like the meme
                            lane; the polymarket_domain_inventory_snapshots
                            coverage table is NOT pruned.)

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
    MarketPriceTickBucket,
    MemeAttentionSnapshot,
    MemeCatalystEvent,
    MemeScoutRun,
    OpportunitySignal,
    PipelineRun,
    PipelineStageRun,
    PolymarketMarket,
    PolymarketOrderbookSnapshot,
    PolymarketScoutRun,
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
    # MEME-NEWS domain-scout inventory is low-frequency (manual report only) —
    # kept as coverage history. The meme attention lane (runs/snapshots/
    # catalysts) IS pruned by MEME_NEWS_RETENTION_DAYS (documented below).
    "domain_scout_runs",
    "domain_market_inventory_snapshots",
    # POLY-001 per-domain inventory is a low-frequency coverage view — kept as
    # history. The Polymarket market/orderbook/run tables ARE pruned by
    # POLYMARKET_RETENTION_DAYS (documented below).
    "polymarket_domain_inventory_snapshots",
)


@dataclass
class RetentionConfig:
    tick_days: int = 7
    watcher_run_days: int = 30
    pipeline_run_days: int = 90
    signal_days: int = 0  # 0 = keep forever
    crypto_days: int = 7  # crypto_price_ticks + crypto_watcher_runs
    meme_days: int = 14  # MEME-NEWS-002: meme_scout_runs + attention + catalysts
    polymarket_days: int = 14  # POLY-001: polymarket_scout_runs + markets + orderbook snaps
    tick_bucket_days: int = 90  # OPS-012: aggregated tick buckets (raw tick_days UNCHANGED)
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
            "meme_days": s.meme_news_retention_days,
            "polymarket_days": s.polymarket_retention_days,
            "tick_bucket_days": s.tick_bucket_retention_days,
            "batch_size": s.retention_batch_size,
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


@dataclass
class TableRetention:
    """Per-table dry-run projection (OPS-011): window, totals, and what a
    prune would remove/keep. Counts only — nothing is deleted."""

    table: str
    window_days: int | None  # None => retained indefinitely (not pruned)
    total_rows: int
    eligible_rows: int
    remaining_rows: int
    oldest: datetime | None = None
    newest: datetime | None = None


class RetentionService:
    def __init__(self, config: RetentionConfig | None = None):
        self.config = config or RetentionConfig.from_settings()

    def prune_report(self, session: Session) -> list[TableRetention]:
        """Detailed, read-only dry-run projection per pruned table: retention
        window, total rows, rows eligible for pruning, rows that would remain,
        plus oldest/newest timestamps for the tick tables. Deletes nothing."""
        cfg = self.config

        def project(model, condition, window_days, timestamps=False) -> TableRetention:
            total = session.execute(select(func.count()).select_from(model)).scalar() or 0
            eligible = session.execute(
                select(func.count()).select_from(model).where(condition)
            ).scalar() or 0
            oldest = newest = None
            if timestamps:
                oldest, newest = session.execute(
                    select(func.min(model.created_at), func.max(model.created_at))
                ).one()
            name = model.__tablename__
            return TableRetention(
                table=name,
                window_days=window_days,
                total_rows=total,
                eligible_rows=eligible,
                remaining_rows=total - eligible,
                oldest=oldest,
                newest=newest,
            )

        reports = [
            project(
                MarketPriceTick,
                MarketPriceTick.created_at < _cutoff(cfg.tick_days),
                cfg.tick_days,
                timestamps=True,
            ),
            # OPS-012 aggregated buckets: their own (much longer) window; the raw
            # tick window above is UNCHANGED by OPS-012.
            project(
                MarketPriceTickBucket,
                MarketPriceTickBucket.created_at < _cutoff(cfg.tick_bucket_days),
                cfg.tick_bucket_days,
                timestamps=True,
            ),
            project(
                CryptoPriceTick,
                CryptoPriceTick.created_at < _cutoff(cfg.crypto_days),
                cfg.crypto_days,
                timestamps=True,
            ),
            project(
                WatcherRun,
                WatcherRun.created_at < _cutoff(cfg.watcher_run_days),
                cfg.watcher_run_days,
            ),
            project(
                CryptoWatcherRun,
                (CryptoWatcherRun.created_at < _cutoff(cfg.crypto_days))
                & (CryptoWatcherRun.status != "running"),
                cfg.crypto_days,
            ),
            project(
                PipelineRun,
                (PipelineRun.created_at < _cutoff(cfg.pipeline_run_days))
                & (PipelineRun.status != "running"),
                cfg.pipeline_run_days,
            ),
            # MEME-NEWS-002 always-on lane (documented pruning; the report /
            # alerts operate on recent windows, so older rows are low-value).
            project(
                MemeAttentionSnapshot,
                MemeAttentionSnapshot.created_at < _cutoff(cfg.meme_days),
                cfg.meme_days,
            ),
            project(
                MemeCatalystEvent,
                MemeCatalystEvent.created_at < _cutoff(cfg.meme_days),
                cfg.meme_days,
            ),
            project(
                MemeScoutRun,
                (MemeScoutRun.created_at < _cutoff(cfg.meme_days))
                & (MemeScoutRun.status != "running"),
                cfg.meme_days,
            ),
            # POLY-001 always-on observer lane (documented pruning; the report /
            # domain view operate on recent windows/the latest run, so older
            # market/orderbook rows are low-value; the domain-inventory table is
            # protected/kept as coverage history).
            project(
                PolymarketMarket,
                PolymarketMarket.created_at < _cutoff(cfg.polymarket_days),
                cfg.polymarket_days,
            ),
            project(
                PolymarketOrderbookSnapshot,
                PolymarketOrderbookSnapshot.created_at < _cutoff(cfg.polymarket_days),
                cfg.polymarket_days,
            ),
            project(
                PolymarketScoutRun,
                (PolymarketScoutRun.created_at < _cutoff(cfg.polymarket_days))
                & (PolymarketScoutRun.status != "running"),
                cfg.polymarket_days,
            ),
        ]
        # opportunity_signals: pruned only when signal_days > 0; else indefinite
        if cfg.signal_days > 0:
            reports.append(
                project(
                    OpportunitySignal,
                    OpportunitySignal.created_at < _cutoff(cfg.signal_days),
                    cfg.signal_days,
                )
            )
        else:
            total = session.execute(
                select(func.count()).select_from(OpportunitySignal)
            ).scalar() or 0
            reports.append(
                TableRetention("opportunity_signals", None, total, 0, total)
            )
        return reports

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
        # OPS-012: aggregated buckets age out on their own much-longer window.
        counts["market_price_tick_buckets"] = self._delete_batched(
            session,
            MarketPriceTickBucket,
            MarketPriceTickBucket.created_at < _cutoff(cfg.tick_bucket_days),
            dry_run,
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

        # MEME-NEWS-002 always-on lane: prune scored snapshots + catalyst
        # events + finished scan runs older than meme_days (documented — the
        # domain-scout inventory tables are protected/kept as coverage history).
        meme_cutoff = _cutoff(cfg.meme_days)
        counts["meme_attention_snapshots"] = self._delete_batched(
            session, MemeAttentionSnapshot, MemeAttentionSnapshot.created_at < meme_cutoff, dry_run
        )
        counts["meme_catalyst_events"] = self._delete_batched(
            session, MemeCatalystEvent, MemeCatalystEvent.created_at < meme_cutoff, dry_run
        )
        counts["meme_scout_runs"] = self._delete_batched(
            session,
            MemeScoutRun,
            (MemeScoutRun.created_at < meme_cutoff) & (MemeScoutRun.status != "running"),
            dry_run,
        )

        # POLY-001 always-on observer lane: prune market + orderbook snapshots +
        # finished scan runs older than polymarket_days (documented — the
        # per-domain inventory table is protected/kept as coverage history).
        polymarket_cutoff = _cutoff(cfg.polymarket_days)
        counts["polymarket_markets"] = self._delete_batched(
            session, PolymarketMarket, PolymarketMarket.created_at < polymarket_cutoff, dry_run
        )
        counts["polymarket_orderbook_snapshots"] = self._delete_batched(
            session,
            PolymarketOrderbookSnapshot,
            PolymarketOrderbookSnapshot.created_at < polymarket_cutoff,
            dry_run,
        )
        counts["polymarket_scout_runs"] = self._delete_batched(
            session,
            PolymarketScoutRun,
            (PolymarketScoutRun.created_at < polymarket_cutoff)
            & (PolymarketScoutRun.status != "running"),
            dry_run,
        )

        if not dry_run and any(counts.values()):
            logger.info("Retention pruned: %s", counts)
        return counts
