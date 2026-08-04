"""OUTCOME-SYNC-COVERAGE-001 — bounded historical outcome synchronization.

The recurring MarketOps outcome stage now selects by need rather than by
alphabetical prefix, so it drains the historical backlog on its own. This
command exists for the case where that is too slow to wait for, and for the
auditability of doing it once, explicitly, under a hard cap.

It fetches through the SAME read-only Kalshi market-detail path MarketOps
already uses and the same `parse_market_outcome` interpreter. It adds no
provider, authorizes no paid provider, and creates no new recurring work.

It never changes a forecast, never infers an outcome from a price or from a
market having closed, never normalizes an unknown status into yes/no, and never
overwrites a row whose stored state contradicts itself.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, MarketForecastRecord, MarketOutcomeRecord
from app.services.outcome_coverage import (
    LOCAL_OUTCOME_CONFLICT,
    MATURITY_GRACE_SECONDS,
    _RECOVERABILITY,
    REQUIRES_CURRENT_PROVIDER_SYNC,
    load_coverage_rows,
)
from app.services.outcomes import OutcomeService, OutcomeSyncError

logger = logging.getLogger(__name__)

DISCLAIMER = (
    "Bounded read-only outcome synchronization — observes settlement, never "
    "advice, EV, a side, a size, an order, a recommendation, or a trade action."
)

# A hard ceiling independent of --max-markets, so a mistyped flag cannot turn a
# bounded repair into an unbounded sweep.
ABSOLUTE_MAX_MARKETS = 2000


@dataclass
class BackfillResult:
    mode: str
    disclaimer: str = DISCLAIMER
    persisted: bool = False
    provider_cap: int = 0
    provider_calls: int = 0
    markets_selected: int = 0
    already_current_excluded: int = 0
    conflicts_excluded: int = 0
    scoring_candidates: int = 0
    outcomes_created: int = 0
    outcomes_refreshed: int = 0
    settled_yes_no: int = 0
    canceled_or_void: int = 0
    still_unsettled: int = 0
    unrecognized_status: int = 0
    provider_failures: int = 0
    scores_created: int = 0
    stop_reason: str = "completed"
    by_reason: dict = field(default_factory=dict)
    examples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def select_backfill_markets(
    session: Session, *, max_markets: int, domain: str | None = None,
    now: datetime | None = None,
) -> tuple[list[str], dict[str, int], int, int]:
    """(tickers, by_reason, already_current, conflicts_excluded).

    Only matured forecasts whose missing reason is recoverable through the
    provider path we already use. Conflicts are excluded by construction: a row
    that disagrees with itself needs a human, not another fetch that would
    overwrite the evidence of the disagreement.
    """
    now = now or datetime.now(timezone.utc)
    rows = load_coverage_rows(session, now=now, domain=domain)
    matured = [r for r in rows if r.matured]

    already_current = sum(1 for r in matured if r.reason is None)
    conflicts = sum(1 for r in matured if r.reason == LOCAL_OUTCOME_CONFLICT)

    by_reason: dict[str, int] = {}
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in matured:
        if row.reason is None or row.reason == LOCAL_OUTCOME_CONFLICT:
            continue
        if _RECOVERABILITY.get(row.reason) != REQUIRES_CURRENT_PROVIDER_SYNC:
            continue
        by_reason[row.reason] = by_reason.get(row.reason, 0) + 1
        if row.market_ticker in seen:
            continue
        seen.add(row.market_ticker)
        # Oldest close first: likeliest to have settled, and the forecasts that
        # have been waiting longest for evidence.
        ranked.append((-(row.close_age_seconds or 0.0), row.market_ticker))

    ranked.sort()
    cap = max(0, min(max_markets, ABSOLUTE_MAX_MARKETS))
    return [t for _, t in ranked[:cap]], by_reason, already_current, conflicts


async def run_backfill(
    session: Session, *, confirm: bool = False, max_markets: int = 250,
    domain: str | None = None, service: OutcomeService | None = None,
    now: datetime | None = None,
) -> BackfillResult:
    now = now or datetime.now(timezone.utc)
    cap = max(0, min(max_markets, ABSOLUTE_MAX_MARKETS))
    tickers, by_reason, already_current, conflicts = select_backfill_markets(
        session, max_markets=cap, domain=domain, now=now)

    result = BackfillResult(
        mode="confirmed" if confirm else "dry_run",
        provider_cap=cap, markets_selected=len(tickers),
        already_current_excluded=already_current, conflicts_excluded=conflicts,
        scoring_candidates=sum(by_reason.values()), by_reason=by_reason,
        examples=tickers[:10],
    )
    if not confirm:
        result.stop_reason = "dry_run"
        return result

    service = service or OutcomeService()
    for ticker in tickers:
        if result.provider_calls >= cap:
            result.stop_reason = "provider_cap"
            break
        existing = session.execute(
            select(MarketOutcomeRecord).where(
                MarketOutcomeRecord.market_ticker == ticker)
        ).scalar_one_or_none()
        had_row = existing is not None
        result.provider_calls += 1
        try:
            row = await service.sync_ticker(session, ticker)
        except OutcomeSyncError as exc:
            # Failure isolation: one unreachable ticker must not end the run or
            # poison the rest of the batch.
            result.provider_failures += 1
            logger.warning("backfill: no detail for %s: %s", ticker, exc)
            continue
        except Exception as exc:  # pragma: no cover - defensive
            session.rollback()
            result.provider_failures += 1
            result.stop_reason = f"error: {type(exc).__name__}"
            break

        if had_row:
            result.outcomes_refreshed += 1
        else:
            result.outcomes_created += 1

        status = (row.outcome_status or "").strip().lower()
        side = (row.winning_side or "").strip().lower()
        if status == "settled" and side in ("yes", "no"):
            result.settled_yes_no += 1
        elif status == "canceled" or side == "void":
            result.canceled_or_void += 1
        elif status == "unknown":
            result.unrecognized_status += 1
        else:
            result.still_unsettled += 1
    else:
        result.stop_reason = "completed"

    result.persisted = result.outcomes_created + result.outcomes_refreshed > 0
    if result.provider_calls and result.provider_failures == result.provider_calls:
        # Every fetch failed. That is the primary signal that the markets we are
        # trying to recover are no longer served at all, which is a different
        # problem from "not synced yet" — it must not exit 0 as "completed".
        result.stop_reason = "all_fetches_failed"
    return result
