"""Market detail enrichment: fetch the richest available Kalshi metadata
(market detail + event + series) for markets that passed the eligibility gate,
so resolution assessment and future forecast agents see named settlement
sources and full rules text instead of the sparse list payload.

Read-only against Kalshi (GETs only); write-only to our own DB."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.models import Market, MarketDetailEnrichment, MarketSnapshot
from app.schemas import MarketData

logger = logging.getLogger(__name__)


class EnrichmentError(RuntimeError):
    """Raised when the market detail payload cannot be fetched at all."""


def _combined_rules_text(market_detail: dict) -> str | None:
    parts = [
        (market_detail.get("rules_primary") or "").strip(),
        (market_detail.get("rules_secondary") or "").strip(),
    ]
    combined = "\n\n".join(part for part in parts if part)
    return combined or None


def _extract_settlement_source(
    market_detail: dict, event_detail: dict | None, series_detail: dict | None
) -> str | None:
    """Named settlement sources, preferring series over event metadata.
    Rendered as 'Name (url); ...' so the rule-based judge and humans can
    both read it."""
    for detail in (series_detail, event_detail, market_detail):
        sources = (detail or {}).get("settlement_sources") or []
        rendered = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            name = (source.get("name") or "").strip()
            url = (source.get("url") or "").strip()
            if name and url:
                rendered.append(f"{name} ({url})")
            elif name or url:
                rendered.append(name or url)
        if rendered:
            return "; ".join(rendered)[:1000]
    return None


def _first_present(*values) -> str | None:
    for value in values:
        if value:
            return value
    return None


class MarketDetailEnrichmentService:
    def __init__(self, adapter: KalshiRestAdapter | None = None):
        self.adapter = adapter or KalshiRestAdapter()

    async def enrich_ticker(
        self,
        session: Session,
        ticker: str,
        scanner_run_id: int | None = None,
    ) -> MarketDetailEnrichment:
        """Fetch detail/event/series metadata for one ticker and persist an
        enrichment row (raw payloads included for audit)."""
        market_detail = await self.adapter.get_market_detail(ticker)
        if market_detail is None:
            raise EnrichmentError(f"Kalshi returned no market detail for {ticker!r}")

        event_ticker = market_detail.get("event_ticker")
        event_detail = (
            await self.adapter.get_event_detail(event_ticker) if event_ticker else None
        )
        series_ticker = market_detail.get("series_ticker") or (event_detail or {}).get(
            "series_ticker"
        )
        series_detail = (
            await self.adapter.get_series_detail(series_ticker) if series_ticker else None
        )

        row = MarketDetailEnrichment(
            market_ticker=ticker,
            scanner_run_id=scanner_run_id,
            event_ticker=event_ticker,
            series_ticker=series_ticker,
            title=_first_present(market_detail.get("title"), (event_detail or {}).get("title")),
            subtitle=_first_present(
                market_detail.get("subtitle"),
                market_detail.get("yes_sub_title"),
                (event_detail or {}).get("sub_title"),
            ),
            rules_text=_combined_rules_text(market_detail),
            settlement_source=_extract_settlement_source(
                market_detail, event_detail, series_detail
            ),
            category=_first_present(
                market_detail.get("category"),
                (event_detail or {}).get("category"),
                (series_detail or {}).get("category"),
            ),
            raw_market_detail=market_detail,
            raw_event_detail=event_detail,
            raw_series_detail=series_detail,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return row

    async def enrich_top_candidates(
        self,
        session: Session,
        run_id: int,
        limit: int = 20,
    ) -> list[MarketDetailEnrichment]:
        """Enrich the top eligible candidates (score > 0) of one scan run.
        Individual fetch failures are logged and skipped, never fatal."""
        rows = session.execute(
            select(MarketSnapshot, Market)
            .join(Market, MarketSnapshot.market_id == Market.id)
            .where(MarketSnapshot.scanner_run_id == run_id, MarketSnapshot.score > 0)
            .order_by(MarketSnapshot.score.desc())
            .limit(limit)
        ).all()

        enriched: list[MarketDetailEnrichment] = []
        for _, market in rows:
            try:
                enriched.append(
                    await self.enrich_ticker(session, market.ticker, scanner_run_id=run_id)
                )
            except EnrichmentError as exc:
                logger.warning("Skipping enrichment for %s: %s", market.ticker, exc)
        return enriched


def latest_enrichment_for(session: Session, ticker: str) -> MarketDetailEnrichment | None:
    return session.execute(
        select(MarketDetailEnrichment)
        .where(MarketDetailEnrichment.market_ticker == ticker)
        .order_by(MarketDetailEnrichment.created_at.desc(), MarketDetailEnrichment.id.desc())
    ).scalars().first()


def apply_enrichment(
    market_data: MarketData, enrichment: MarketDetailEnrichment | None
) -> MarketData:
    """Overlay one enrichment row onto a MarketData: enriched rules_text and
    settlement_source win over list-level fields; list-level values remain the
    fallback. Pure function."""
    if enrichment is None:
        return market_data
    return market_data.model_copy(
        update={
            "rules_primary": enrichment.rules_text or market_data.rules_primary,
            "settlement_source": enrichment.settlement_source,
            "title": enrichment.title or market_data.title,
            "category": enrichment.category or market_data.category,
        }
    )


def apply_latest_enrichment(session: Session, market_data: MarketData) -> MarketData:
    """Overlay the latest persisted enrichment (if any) onto a MarketData.
    Deterministic given the DB state."""
    return apply_enrichment(market_data, latest_enrichment_for(session, market_data.ticker))
