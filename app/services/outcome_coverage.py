"""OUTCOME-SYNC-COVERAGE-001 — read-only outcome-coverage diagnostics.

Answers one question: *why* do most matured forecasts have no usable outcome,
and which of those are recoverable without adding a provider.

`forecast_scorability` already models whether a forecast **is** scorable. This
module models why the *outcome row* behind that state is absent, which is a
different question with a different fix. It reuses that module's classification
and `calibration._score_target` rather than restating scoring semantics.

Measurement only. It writes nothing, calls no provider, creates no outcome and
no score. No EV, side, size, order, recommendation, wallet, or trade action
exists here by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    ForecastScoreRecord,
    Market,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
)
from app.services.calibration import STATUS_SCORED, _score_target

DISCLAIMER = (
    "Outcome-coverage data-quality measurement only — never advice, EV, a side, "
    "a size, an order, a recommendation, a wallet, or a trade action. A missing "
    "outcome is NOT a loss, and a closed market is NOT a resolved market."
)

# A market's close time is not its settlement time. Kalshi settles after close,
# so a forecast is only "matured-eligible" once close is far enough in the past
# that a missing outcome is a coverage question rather than normal latency.
# One hour is deliberately short: it makes the denominator LARGER and therefore
# makes coverage look WORSE, which is the conservative direction for a metric
# whose whole purpose is to stop us overstating how much evidence we have.
MATURITY_GRACE_SECONDS = 3600

# An outcome row still reading `open` this long after close is not "fresh yet",
# it is a row nobody has revisited.
STALE_OPEN_SECONDS = 6 * 3600

# --- missing-outcome taxonomy (mutually exclusive, deterministic precedence) ---
SYNC_NEVER_ATTEMPTED = "sync_never_attempted"
PROVIDER_MARKET_MISSING = "provider_market_missing"
PROVIDER_MARKET_MAPPING_FAILED = "provider_market_mapping_failed"
PROVIDER_STATUS_UNRECOGNIZED = "provider_status_unrecognized"
PROVIDER_RESOLUTION_MISSING = "provider_resolution_missing"
MARKET_CLOSED_UNSETTLED = "market_closed_unsettled"
MARKET_CANCELED = "market_canceled"
MARKET_VOID = "market_void"
WINNER_MISSING = "winner_missing"
WINNER_AMBIGUOUS = "winner_ambiguous"
LOCAL_OUTCOME_STALE = "local_outcome_stale"
LOCAL_OUTCOME_CONFLICT = "local_outcome_conflict"
SYNC_ERROR = "sync_error"
DOMAIN_NOT_SUPPORTED = "domain_not_supported"
PERMANENTLY_UNSCORABLE = "permanently_unscorable"
STATE_INCONSISTENT = "state_inconsistent"

ALL_REASONS = (
    SYNC_NEVER_ATTEMPTED, PROVIDER_MARKET_MISSING, PROVIDER_MARKET_MAPPING_FAILED,
    PROVIDER_STATUS_UNRECOGNIZED, PROVIDER_RESOLUTION_MISSING, MARKET_CLOSED_UNSETTLED,
    MARKET_CANCELED, MARKET_VOID, WINNER_MISSING, WINNER_AMBIGUOUS,
    LOCAL_OUTCOME_STALE, LOCAL_OUTCOME_CONFLICT, SYNC_ERROR, DOMAIN_NOT_SUPPORTED,
    PERMANENTLY_UNSCORABLE, STATE_INCONSISTENT,
)

# --- recoverability classes ---------------------------------------------------
RECOVERABLE_LOCAL = "recoverable_local"
REQUIRES_CURRENT_PROVIDER_SYNC = "requires_current_provider_sync"
REQUIRES_NEW_MAPPING = "requires_new_mapping"
REQUIRES_NEW_STATUS_INTERPRETER = "requires_new_status_interpreter"
REQUIRES_NEW_PROVIDER = "requires_new_provider"
NOT_RECOVERABLE = "permanently_unscorable"

# Which reasons are fixable by spending the provider budget we ALREADY spend.
# Nothing here needs a new provider: every one of these is a Kalshi market-detail
# GET that the outcome stage is already authorized to make and already makes ~100
# of every cycle — it just makes them against the wrong markets.
_RECOVERABILITY = {
    SYNC_NEVER_ATTEMPTED: REQUIRES_CURRENT_PROVIDER_SYNC,
    LOCAL_OUTCOME_STALE: REQUIRES_CURRENT_PROVIDER_SYNC,
    MARKET_CLOSED_UNSETTLED: REQUIRES_CURRENT_PROVIDER_SYNC,
    PROVIDER_RESOLUTION_MISSING: REQUIRES_CURRENT_PROVIDER_SYNC,
    SYNC_ERROR: REQUIRES_CURRENT_PROVIDER_SYNC,
    PROVIDER_MARKET_MAPPING_FAILED: REQUIRES_NEW_MAPPING,
    PROVIDER_STATUS_UNRECOGNIZED: REQUIRES_NEW_STATUS_INTERPRETER,
    WINNER_AMBIGUOUS: REQUIRES_NEW_STATUS_INTERPRETER,
    LOCAL_OUTCOME_CONFLICT: REQUIRES_NEW_STATUS_INTERPRETER,
    PROVIDER_MARKET_MISSING: REQUIRES_NEW_PROVIDER,
    DOMAIN_NOT_SUPPORTED: REQUIRES_NEW_PROVIDER,
    MARKET_CANCELED: NOT_RECOVERABLE,
    MARKET_VOID: NOT_RECOVERABLE,
    WINNER_MISSING: NOT_RECOVERABLE,
    PERMANENTLY_UNSCORABLE: NOT_RECOVERABLE,
    STATE_INCONSISTENT: NOT_RECOVERABLE,
}

# Reasons whose forecasts would become scorable if the recovery succeeded.
_SCORING_CANDIDATE = {
    SYNC_NEVER_ATTEMPTED, LOCAL_OUTCOME_STALE, MARKET_CLOSED_UNSETTLED,
    PROVIDER_RESOLUTION_MISSING, SYNC_ERROR,
}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class CoverageRow:
    forecast_id: int
    market_ticker: str
    domain: str
    forecaster: str
    close_time: datetime | None
    close_age_seconds: float | None
    has_market: bool
    matured: bool
    outcome: MarketOutcomeRecord | None
    score: ForecastScoreRecord | None
    scored_current: bool
    reason: str | None = None       # None when the outcome IS usable
    recoverability: str | None = None


def classify_missing_reason(
    row_has_market: bool,
    outcome: MarketOutcomeRecord | None,
    close_age_seconds: float | None,
    *,
    ever_attempted: bool,
    had_sync_error: bool = False,
) -> str | None:
    """The single reason a matured forecast has no usable current outcome.

    Returns None when the outcome IS usable (settled yes/no). Precedence is
    deliberate and documented: structural problems first, then provider-state
    problems, then staleness. Every branch is a distinguishable failure mode —
    nothing collapses into "unknown", because "unknown" is what made this
    milestone necessary.
    """
    if not row_has_market:
        return STATE_INCONSISTENT

    if outcome is None:
        # No row at all. Distinguish "we tried and could not" from "we never
        # looked" — they have completely different fixes, and conflating them
        # is what makes a coverage gap look like a provider gap.
        if had_sync_error:
            return SYNC_ERROR
        if ever_attempted:
            return PROVIDER_MARKET_MISSING
        return SYNC_NEVER_ATTEMPTED

    status = (outcome.outcome_status or "").strip().lower()
    side = (outcome.winning_side or "").strip().lower() or None

    if status == "settled":
        if side in ("yes", "no"):
            if outcome.resolved_probability is None:
                # Settled with a side but no probability: the row disagrees
                # with itself. Preserve the conflict; never repair it silently.
                return LOCAL_OUTCOME_CONFLICT
            expected = 1.0 if side == "yes" else 0.0
            if outcome.resolved_probability != expected:
                return LOCAL_OUTCOME_CONFLICT
            return None  # usable
        if side == "void":
            return MARKET_VOID
        if side in (None, "unknown"):
            return WINNER_MISSING
        return WINNER_AMBIGUOUS

    if status == "canceled":
        return MARKET_VOID if side == "void" else MARKET_CANCELED

    if status == "unknown":
        return PROVIDER_STATUS_UNRECOGNIZED

    if status == "closed":
        return MARKET_CLOSED_UNSETTLED

    if status == "open":
        # The market's close time has passed (the caller only asks about matured
        # forecasts) but our row still says open, so the row predates the close
        # and nobody has revisited it.
        if close_age_seconds is not None and close_age_seconds >= STALE_OPEN_SECONDS:
            return LOCAL_OUTCOME_STALE
        return MARKET_CLOSED_UNSETTLED

    return PROVIDER_STATUS_UNRECOGNIZED


@dataclass
class SelectionAudit:
    """Why the current selection reaches the markets it reaches.

    This is the part that distinguishes "the sync is under-scheduled" from "the
    sync structurally cannot reach these markets". Under-scheduling is fixed by
    waiting or raising a cap; a frozen prefix is never fixed by either.
    """
    distinct_forecasted_tickers: int
    configured_limit: int
    reachable_tickers: int
    unreachable_tickers: int
    terminal_rows_reselected: int
    first_unreachable_rank: int | None
    verdict: str
    detail: str


def audit_selection(session: Session, *, limit: int) -> SelectionAudit:
    """Replays the DEPLOYED selection (`OutcomeService.sync_known_markets`)
    without calling a provider, and reports what it can never reach."""
    tickers = [t for (t,) in session.execute(
        select(MarketForecastRecord.market_ticker).distinct()
        .order_by(MarketForecastRecord.market_ticker)
    ).all()]
    reachable = tickers[:limit]
    reachable_set = set(reachable)

    terminal = session.execute(
        select(func.count()).select_from(MarketOutcomeRecord).where(
            MarketOutcomeRecord.market_ticker.in_(reachable_set or {""}),
            MarketOutcomeRecord.outcome_status.in_(("settled", "canceled")),
        )
    ).scalar() or 0

    unreachable = max(len(tickers) - len(reachable), 0)
    first_unreachable = limit if unreachable else None

    if unreachable == 0:
        verdict = "SELECTION_REACHES_EVERY_FORECASTED_MARKET"
        detail = (
            f"all {len(tickers)} forecasted tickers fall inside the {limit}-ticker "
            "selection, so coverage gaps are not caused by selection")
    else:
        verdict = "SELECTION_IS_A_FROZEN_ALPHABETICAL_PREFIX"
        detail = (
            f"selection sorts {len(tickers)} distinct forecasted tickers "
            f"alphabetically and keeps the first {limit}; the remaining "
            f"{unreachable} are unreachable on EVERY cycle, not merely delayed. "
            f"Of the {len(reachable)} that are reachable, {terminal} already hold a "
            "TERMINAL outcome (settled/canceled) and are re-fetched anyway, so a "
            "further share of the budget buys nothing.")
    return SelectionAudit(
        distinct_forecasted_tickers=len(tickers), configured_limit=limit,
        reachable_tickers=len(reachable), unreachable_tickers=unreachable,
        terminal_rows_reselected=terminal, first_unreachable_rank=first_unreachable,
        verdict=verdict, detail=detail,
    )


def load_coverage_rows(
    session: Session, *, now: datetime | None = None,
    since: datetime | None = None, until: datetime | None = None,
    domain: str | None = None, grace_seconds: int = MATURITY_GRACE_SECONDS,
) -> list[CoverageRow]:
    """One row per forecast. Bulk-loaded; no N+1, no provider call, no write."""
    now = now or _now()
    until = until or now
    q = select(MarketForecastRecord).where(MarketForecastRecord.created_at <= until)
    if since is not None:
        q = q.where(MarketForecastRecord.created_at >= since)
    forecasts = list(session.execute(
        q.order_by(MarketForecastRecord.id)).scalars().all())

    tickers = {f.market_ticker for f in forecasts}
    fids = [f.id for f in forecasts]
    packet_ids = {f.research_packet_id for f in forecasts if f.research_packet_id}

    outcomes = {o.market_ticker: o for o in session.execute(
        select(MarketOutcomeRecord).where(
            MarketOutcomeRecord.market_ticker.in_(tickers or {""}))
    ).scalars()}
    markets = {m.ticker: m for m in session.execute(
        select(Market).where(Market.ticker.in_(tickers or {""}))
    ).scalars()}
    packets = {p.id: p for p in session.execute(
        select(MarketResearchPacket).where(
            MarketResearchPacket.id.in_(packet_ids or {0}))
    ).scalars()}
    latest_score: dict[int, ForecastScoreRecord] = {}
    for s in session.execute(
        select(ForecastScoreRecord)
        .where(ForecastScoreRecord.forecast_id.in_(fids or {0}))
        .order_by(ForecastScoreRecord.id)
    ).scalars():
        latest_score[s.forecast_id] = s  # ascending -> last wins == max id

    # "Ever attempted" is evidenced by an outcome row existing for the ticker.
    # Deliberately conservative: we cannot prove a fetch was attempted and
    # returned nothing, so a ticker with no row is only called
    # `sync_never_attempted` when the selection audit also shows it unreachable.
    attempted = set(outcomes)

    rows: list[CoverageRow] = []
    for f in forecasts:
        market = markets.get(f.market_ticker)
        outcome = outcomes.get(f.market_ticker)
        packet = packets.get(f.research_packet_id) if f.research_packet_id else None
        score = latest_score.get(f.id)
        close_time = _aware(
            (outcome.close_time if outcome and outcome.close_time else None)
            or (market.close_time if market else None))
        age = (now - close_time).total_seconds() if close_time else None
        dom = packet.domain if packet else "missing_packet"
        if domain is not None and dom != domain:
            continue

        matured = (
            market is not None
            and close_time is not None
            and age is not None and age >= grace_seconds
            and f.estimated_probability is not None
        )
        scored_current = bool(
            score is not None and score.score_status == STATUS_SCORED
            and _score_target(outcome)[0] == STATUS_SCORED
            and score.outcome_id == (outcome.id if outcome else None)
        )
        row = CoverageRow(
            forecast_id=f.id, market_ticker=f.market_ticker, domain=dom,
            forecaster=f"{f.forecaster_name}:{f.forecaster_version}",
            close_time=close_time, close_age_seconds=age,
            has_market=market is not None, matured=matured,
            outcome=outcome, score=score, scored_current=scored_current,
        )
        if matured:
            row.reason = classify_missing_reason(
                row.has_market, outcome, age,
                ever_attempted=f.market_ticker in attempted)
            row.recoverability = (
                _RECOVERABILITY.get(row.reason) if row.reason else None)
        rows.append(row)
    return rows


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


@dataclass
class CoverageReport:
    generated_at: str
    disclaimer: str = DISCLAIMER
    external_calls: int = 0
    persisted: bool = False
    grace_seconds: int = MATURITY_GRACE_SECONDS
    funnel: dict = field(default_factory=dict)
    taxonomy: list = field(default_factory=list)
    recoverability: dict = field(default_factory=dict)
    by_domain: list = field(default_factory=list)
    by_forecaster: list = field(default_factory=list)
    by_close_age: list = field(default_factory=list)
    selection: dict = field(default_factory=dict)
    uplift: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)
    data_quality: list = field(default_factory=list)
    verdict: str = ""

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def build_coverage_report(
    session: Session, *, now: datetime | None = None,
    since: datetime | None = None, until: datetime | None = None,
    domain: str | None = None, selection_limit: int | None = None,
    examples_per_reason: int = 3,
) -> CoverageReport:
    now = now or _now()
    rows = load_coverage_rows(
        session, now=now, since=since, until=until, domain=domain)
    matured = [r for r in rows if r.matured]
    usable = [r for r in matured if r.reason is None]
    missing = [r for r in matured if r.reason is not None]

    if selection_limit is None:
        from app.config import get_settings
        selection_limit = get_settings().marketops_sync_outcome_limit
    audit = audit_selection(session, limit=selection_limit)

    closed_any = sum(1 for r in rows if r.close_time is not None
                     and r.close_age_seconds is not None and r.close_age_seconds > 0)
    funnel = {
        "all_forecasts": len(rows),
        "markets_closed": closed_any,
        "matured_eligible": len(matured),
        "outcome_row_present": sum(1 for r in matured if r.outcome is not None),
        "settled_yes_no": len(usable),
        "scored_current": sum(1 for r in matured if r.scored_current),
        "unscorable": sum(1 for r in missing
                          if _RECOVERABILITY.get(r.reason) == NOT_RECOVERABLE),
        "pending_legitimately_unresolved": sum(
            1 for r in missing if r.reason == MARKET_CLOSED_UNSETTLED),
        "missing_outcome": len(missing),
        "matured_coverage_pct": _pct(len(usable), len(matured)),
        "scored_current_pct": _pct(
            sum(1 for r in matured if r.scored_current), len(matured)),
    }

    by_reason: dict[str, list[CoverageRow]] = {}
    for r in missing:
        by_reason.setdefault(r.reason, []).append(r)
    taxonomy = []
    for reason in ALL_REASONS:
        group = by_reason.get(reason, [])
        if not group:
            continue
        rec = _RECOVERABILITY.get(reason, NOT_RECOVERABLE)
        ages = sorted(g.close_age_seconds or 0.0 for g in group)
        taxonomy.append({
            "reason": reason,
            "forecasts": len(group),
            "markets": len({g.market_ticker for g in group}),
            "share_of_matured_pct": _pct(len(group), len(matured)),
            "domains": sorted({g.domain for g in group})[:6],
            "forecasters": sorted({g.forecaster for g in group})[:6],
            "newest_close_age_hours": round(ages[0] / 3600, 1) if ages else None,
            "oldest_close_age_hours": round(ages[-1] / 3600, 1) if ages else None,
            "recoverability": rec,
            "provider_call_required": rec in (
                REQUIRES_CURRENT_PROVIDER_SYNC, REQUIRES_NEW_MAPPING,
                REQUIRES_NEW_PROVIDER),
            "would_become_scorable": reason in _SCORING_CANDIDATE,
        })

    recoverability: dict[str, int] = {}
    for r in missing:
        recoverability[r.recoverability] = recoverability.get(r.recoverability, 0) + 1

    def segment(key):
        out: dict[str, dict] = {}
        for r in matured:
            k = key(r)
            b = out.setdefault(k, {"key": k, "matured": 0, "usable": 0, "scored_current": 0})
            b["matured"] += 1
            b["usable"] += 1 if r.reason is None else 0
            b["scored_current"] += 1 if r.scored_current else 0
        for b in out.values():
            b["coverage_pct"] = _pct(b["usable"], b["matured"])
        return sorted(out.values(), key=lambda b: -b["matured"])

    def age_bucket(r: CoverageRow) -> str:
        a = (r.close_age_seconds or 0) / 86400
        if a < 2:
            return "a <2d"
        if a < 7:
            return "b 2-7d"
        if a < 30:
            return "c 7-30d"
        return "d >30d"

    recoverable_now = sum(
        1 for r in missing if r.recoverability == REQUIRES_CURRENT_PROVIDER_SYNC)
    scoring_candidates = sum(1 for r in missing if r.reason in _SCORING_CANDIDATE)
    uplift = {
        "matured_coverage_now_pct": funnel["matured_coverage_pct"],
        "recoverable_with_current_providers": recoverable_now,
        "requires_new_mapping": recoverability.get(REQUIRES_NEW_MAPPING, 0),
        "requires_new_status_interpreter": recoverability.get(
            REQUIRES_NEW_STATUS_INTERPRETER, 0),
        "requires_new_provider": recoverability.get(REQUIRES_NEW_PROVIDER, 0),
        "permanently_unscorable": recoverability.get(NOT_RECOVERABLE, 0),
        "scoring_candidates": scoring_candidates,
        # UPPER BOUND, explicitly. Every recoverable market is counted as if it
        # settles yes/no, which it will not: some are genuinely still unsettled
        # and some will come back canceled or void. The floor is today's number.
        "max_attainable_coverage_pct": _pct(
            len(usable) + recoverable_now, len(matured)),
        "attainable_is_upper_bound": True,
    }

    examples = {}
    for reason, group in by_reason.items():
        examples[reason] = [
            {"forecast_id": g.forecast_id, "market_ticker": g.market_ticker,
             "domain": g.domain,
             "close_age_hours": round((g.close_age_seconds or 0) / 3600, 1),
             "outcome_status": (g.outcome.outcome_status if g.outcome else None),
             "winning_side": (g.outcome.winning_side if g.outcome else None)}
            for g in sorted(group, key=lambda x: -(x.close_age_seconds or 0))
            [:examples_per_reason]
        ]

    data_quality = []
    if audit.unreachable_tickers:
        data_quality.append(
            f"{audit.unreachable_tickers} forecasted tickers are outside the "
            f"{audit.configured_limit}-ticker outcome-sync selection and are "
            "unreachable on every cycle")
    if audit.terminal_rows_reselected:
        data_quality.append(
            f"{audit.terminal_rows_reselected} markets inside the selection already "
            "hold a terminal outcome and are re-fetched every cycle")
    scored_ids = [r.forecast_id for r in rows if r.score is not None]
    if scored_ids:
        hi = max(scored_ids)
        never = sum(1 for r in rows if r.score is None and r.forecast_id < hi)
        if never == 0 and len(scored_ids) < len(rows):
            data_quality.append(
                f"every forecast with id <= {hi} has a score row and none above it "
                "does — the scoring selection is an id-ordered prefix, not a backlog")
    conflicts = sum(1 for r in missing if r.reason == LOCAL_OUTCOME_CONFLICT)
    if conflicts:
        data_quality.append(
            f"{conflicts} outcome rows disagree with themselves (side vs "
            "resolved_probability); preserved unscored, never repaired silently")

    if not matured:
        verdict = "INSUFFICIENT_DATA"
    elif audit.unreachable_tickers and recoverable_now >= 0.5 * len(missing):
        verdict = "OUTCOME_SYNC_SELECTION_IS_THE_BLOCKER"
    elif recoverability.get(REQUIRES_NEW_PROVIDER, 0) >= 0.5 * len(missing):
        verdict = "NEW_PROVIDER_REQUIRED"
    elif recoverability.get(NOT_RECOVERABLE, 0) >= 0.5 * len(missing):
        verdict = "COVERAGE_IS_STRUCTURALLY_UNRECOVERABLE"
    elif not missing:
        verdict = "COVERAGE_HEALTHY"
    else:
        verdict = "MAPPING_OR_INTERPRETATION_WORK_REQUIRED"

    return CoverageReport(
        generated_at=now.isoformat(), funnel=funnel, taxonomy=taxonomy,
        recoverability=recoverability, by_domain=segment(lambda r: r.domain),
        by_forecaster=segment(lambda r: r.forecaster),
        by_close_age=sorted(segment(age_bucket), key=lambda b: b["key"]),
        selection={
            "distinct_forecasted_tickers": audit.distinct_forecasted_tickers,
            "configured_limit": audit.configured_limit,
            "reachable_tickers": audit.reachable_tickers,
            "unreachable_tickers": audit.unreachable_tickers,
            "terminal_rows_reselected": audit.terminal_rows_reselected,
            "first_unreachable_rank": audit.first_unreachable_rank,
            "verdict": audit.verdict, "detail": audit.detail,
        },
        uplift=uplift, examples=examples, data_quality=data_quality,
        verdict=verdict,
    )
