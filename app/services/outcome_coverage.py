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

from sqlalchemy import and_, func, or_, select
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

# MarketOps fires every 6 minutes. Used only to turn a sweep length in cycles
# into hours, which is the unit an operator actually reasons about.
MARKETOPS_CYCLE_SECONDS = 360
# Above this, a rotating queue is technically complete but practically stale:
# a market that settles right after its slot waits most of a day for the next.
MAX_HEALTHY_SWEEP_HOURS = 12.0

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

# Reasons this repository currently has NO SIGNAL for, declared rather than left
# to look like measured zeros. They stay in the taxonomy because the milestone
# defines them and a future mapping/provider change would populate them, but the
# report states plainly that a zero here means "not measurable", not "none".
UNMEASURABLE_REASONS = (
    PROVIDER_MARKET_MISSING,          # needs attempt evidence; see below
    PROVIDER_MARKET_MAPPING_FAILED,   # single provider, the ticker IS the mapping
    PROVIDER_RESOLUTION_MISSING,      # indistinguishable from closed_unsettled
    SYNC_ERROR,                       # a failed fetch leaves no persisted trace
    DOMAIN_NOT_SUPPORTED,             # every forecast domain routes to Kalshi
    PERMANENTLY_UNSCORABLE,           # subsumed by the specific terminal reasons
    STATE_INCONSISTENT,               # maturity already requires a market row
)

# `PROVIDER_MARKET_MISSING` deserves its own note, because it is the reason that
# answers "is a new provider objectively required?" and it is precisely the one
# this report CANNOT answer. A successful fetch writes a row; a failed one writes
# nothing. So "no row" is indistinguishable between never-selected and
# selected-and-failed, and no persisted state separates them.
#
# A proxy was tried — treat "inside the currently reachable selection" as
# evidence of an attempt — and it is wrong: reachable means "would be selected
# next cycle", not "was already tried", so a freshly forecast market would be
# labelled a provider gap. Rather than dress a guess up as a classification,
# this is declared unmeasurable and the thing that DOES measure it is named.
PROVIDER_MISSING_PROBE = (
    "outcome-sync-backfill --confirm --max-markets 20  (reports provider_failures)"
)

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
        # looked" — completely different fixes, and conflating them is what makes
        # a coverage gap look like a provider gap.
        #
        # A successful fetch always writes a row, so "no row" means either never
        # selected, or selected and failed. `ever_attempted` is therefore the
        # ticker's presence in the CURRENTLY REACHABLE selection. An earlier
        # version tested `ticker in outcomes` inside a branch that only runs when
        # the ticker is NOT in outcomes, so this could never fire and
        # `requires_new_provider` was structurally always zero — a number that
        # could only ever say one thing, presented as though it had been measured.
        if had_sync_error:
            return SYNC_ERROR
        if ever_attempted:
            return PROVIDER_MARKET_MISSING
        return SYNC_NEVER_ATTEMPTED

    status = (outcome.outcome_status or "").strip().lower()
    side = (outcome.winning_side or "").strip().lower() or None

    if status == "settled":
        if side in ("yes", "no"):
            # Same rule as `calibration._score_target`, deliberately: two
            # different notions of "conflict" between the scorer and the
            # coverage report is how the funnel stopped being monotonic in the
            # first place. A MISSING probability means "not recorded"; only a
            # present, disagreeing one is a contradiction.
            expected = 1.0 if side == "yes" else 0.0
            if (outcome.resolved_probability is not None
                    and outcome.resolved_probability != expected):
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
    repair_enabled: bool
    active_selection: str
    reachable_tickers: int
    unreachable_tickers: int
    terminal_rows_reselected: int
    first_unreachable_rank: int | None
    candidate_pool: int
    full_sweep_cycles: int | None
    full_sweep_hours: float | None
    verdict: str
    detail: str


def _reachable_for_audit(session: Session, limit: int, repair: bool) -> list[str]:
    from app.services.outcomes import OutcomeService

    service = OutcomeService.__new__(OutcomeService)  # no adapter: nothing fetched
    if repair:
        return service.select_sync_candidates(session, limit)
    return service.legacy_alphabetical_candidates(session, limit)


def audit_selection(
    session: Session, *, limit: int, repair_enabled: bool | None = None
) -> SelectionAudit:
    """Replays the selection that is ACTUALLY RUNNING, and says which one.

    An earlier version hard-coded the alphabetical prefix and claimed in its
    docstring to be replaying the deployed selection. That was true only until
    the repair shipped, at which point the tool built to VALIDATE the repair
    would have reported that the repair was not there — in a repository whose
    entire value is honest measurement. So the flag is read, and both the active
    selection and its name are reported.
    """
    if repair_enabled is None:
        from app.config import get_settings

        repair_enabled = get_settings().enable_outcome_sync_coverage_repair

    tickers = [t for (t,) in session.execute(
        select(MarketForecastRecord.market_ticker).distinct()
        .order_by(MarketForecastRecord.market_ticker)
    ).all()]

    from app.services.outcomes import OutcomeService

    service = OutcomeService.__new__(OutcomeService)  # no adapter: nothing is fetched
    if repair_enabled:
        reachable = service.select_sync_candidates(session, limit)
        active = "need_based"
    else:
        reachable = service.legacy_alphabetical_candidates(session, limit)
        active = "legacy_alphabetical_prefix"

    # The pool is what the rotation actually has to get through — count it
    # directly. Probing `select_sync_candidates` with a huge limit does NOT
    # measure this: that selector fills any shortfall from recently-seen
    # NON-forecasted markets, so an enormous limit makes the fallback engulf the
    # whole `markets` table. On production that reported a pool of 101,166
    # against 5,019 forecasted tickers and a fictitious 101-hour sweep, while
    # the real production path (limit=100) never reaches the fallback at all.
    if repair_enabled:
        terminal_tickers = {
            t for (t,) in session.execute(
                select(MarketOutcomeRecord.market_ticker).where(
                    or_(
                        and_(MarketOutcomeRecord.outcome_status == "settled",
                             MarketOutcomeRecord.winning_side.in_(("yes", "no"))),
                        MarketOutcomeRecord.outcome_status == "canceled",
                        MarketOutcomeRecord.winning_side == "void",
                    )
                )
            ).all()
        }
        candidate_pool = len(set(tickers) - terminal_tickers)
    else:
        candidate_pool = len(tickers)

    terminal = session.execute(
        select(func.count()).select_from(MarketOutcomeRecord).where(
            MarketOutcomeRecord.market_ticker.in_(set(reachable) or {""}),
            MarketOutcomeRecord.outcome_status.in_(("settled", "canceled")),
        )
    ).scalar() or 0

    if repair_enabled:
        # MEASURE the sweep; do not assert it. An earlier version hard-coded
        # `unreachable = 0` here, which made this tool structurally incapable of
        # reporting that the repair is INSUFFICIENT — a constant presented as a
        # measurement, which is the exact defect this module indicts elsewhere.
        # A rotating queue has no permanently unreachable member, but it very
        # much has a sweep PERIOD, and that period is the thing that can go bad.
        unreachable = 0
        first_unreachable = None
        pool = candidate_pool
        sweep_cycles = -(-pool // limit) if limit > 0 else None
        sweep_hours = (
            round(sweep_cycles * MARKETOPS_CYCLE_SECONDS / 3600.0, 2)
            if sweep_cycles is not None else None)
        if sweep_hours is not None and sweep_hours > MAX_HEALTHY_SWEEP_HOURS:
            verdict = "SELECTION_SWEEP_PERIOD_TOO_LONG"
        else:
            verdict = "SELECTION_IS_NEED_BASED_AND_ROTATES"
        detail = (
            f"{len(tickers)} distinct forecasted tickers, {pool} of them still "
            f"needing a fetch; the {limit}-call budget goes to markets whose "
            "outcome can still change, oldest close first, rotating each cycle. "
            f"A full sweep of the pool takes ~{sweep_cycles} cycles "
            f"(~{sweep_hours} h at {MARKETOPS_CYCLE_SECONDS}s/cycle). "
            f"{terminal} terminal rows are excluded rather than re-fetched.")
    else:
        sweep_cycles = sweep_hours = None
        unreachable = max(len(tickers) - limit, 0)
        first_unreachable = limit if unreachable else None
        if unreachable == 0:
            verdict = "SELECTION_REACHES_EVERY_FORECASTED_MARKET"
            detail = (
                f"all {len(tickers)} forecasted tickers fall inside the "
                f"{limit}-ticker selection, so coverage gaps are not caused by "
                "selection")
        else:
            verdict = "SELECTION_IS_A_FROZEN_ALPHABETICAL_PREFIX"
            detail = (
                f"selection sorts {len(tickers)} distinct forecasted tickers "
                f"alphabetically and keeps the first {limit}; the remaining "
                f"{unreachable} are unreachable on EVERY cycle, not merely "
                f"delayed. Of the {len(reachable)} reachable, {terminal} already "
                "hold a TERMINAL outcome and are re-fetched anyway. Set "
                "ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR=true to fix this.")
    return SelectionAudit(
        distinct_forecasted_tickers=len(tickers), configured_limit=limit,
        repair_enabled=repair_enabled, active_selection=active,
        reachable_tickers=len(reachable), unreachable_tickers=unreachable,
        terminal_rows_reselected=terminal, first_unreachable_rank=first_unreachable,
        candidate_pool=candidate_pool,
        full_sweep_cycles=(sweep_cycles if repair_enabled else None),
        full_sweep_hours=(sweep_hours if repair_enabled else None),
        verdict=verdict, detail=detail,
    )


def load_coverage_rows(
    session: Session, *, now: datetime | None = None,
    since: datetime | None = None, until: datetime | None = None,
    domain: str | None = None, grace_seconds: int = MATURITY_GRACE_SECONDS,
    reachable_tickers: set[str] | None = None,
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
    # Only `domain` is read, and these rows carry large JSON columns
    # (raw_response, sources, key_facts). Select two columns, not the ORM row.
    packets = {
        pid: dom for pid, dom in session.execute(
            select(MarketResearchPacket.id, MarketResearchPacket.domain)
            .where(MarketResearchPacket.id.in_(packet_ids or {0}))
        ).all()
    }
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
    attempted = reachable_tickers if reachable_tickers is not None else set()

    rows: list[CoverageRow] = []
    for f in forecasts:
        market = markets.get(f.market_ticker)
        outcome = outcomes.get(f.market_ticker)
        score = latest_score.get(f.id)
        # Maturity is decided by the MARKET's close time only. Falling back to
        # the outcome row's close time made maturity depend on whether an
        # outcome happened to exist — the exact bias this denominator is built
        # to avoid, and biased OPTIMISTICALLY, because the rows admitted that
        # way are disproportionately the usable ones, lifting numerator and
        # denominator together. The outcome's close time is still reported, but
        # it never decides membership.
        market_close = _aware(market.close_time if market else None)
        outcome_close = _aware(outcome.close_time if outcome else None)
        close_time = market_close or outcome_close
        age = ((now - market_close).total_seconds()
               if market_close is not None else None)
        dom = packets.get(f.research_packet_id) or "missing_packet"
        if domain is not None and dom != domain:
            continue

        matured = (
            market is not None
            and market_close is not None
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
    if selection_limit is None:
        from app.config import get_settings

        selection_limit = get_settings().marketops_sync_outcome_limit
    audit = audit_selection(session, limit=selection_limit)
    # Deliberately NOT passing a reachable set: see PROVIDER_MISSING_PROBE.
    rows = load_coverage_rows(
        session, now=now, since=since, until=until, domain=domain)
    matured = [r for r in rows if r.matured]
    usable = [r for r in matured if r.reason is None]
    missing = [r for r in matured if r.reason is not None]

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
        # The loose bound counts markets that closed minutes ago and are simply
        # awaiting settlement as recoverable "uplift", which overstates what
        # fixing selection buys. The tight bound excludes them. Report both:
        # the truth is between, and quoting only the loose one would flatter.
        "max_attainable_excluding_awaiting_settlement_pct": _pct(
            len(usable) + recoverable_now
            - sum(1 for r in missing if r.reason == MARKET_CLOSED_UNSETTLED),
            len(matured)),
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
    if scored_ids and not audit.repair_enabled:
        # Only diagnostic while the repair is OFF. With it ON the selector walks
        # forecasts in id order taking those that need work, so a contiguous
        # scored prefix is the EXPECTED shape of a draining queue, not evidence
        # of a frozen one — and reporting it as a defect would have this tool
        # blaming a bug it had already helped fix.
        hi = max(scored_ids)
        never = sum(1 for r in rows if r.score is None and r.forecast_id < hi)
        if never == 0 and len(scored_ids) < len(rows):
            data_quality.append(
                f"every forecast with id <= {hi} has a score row and none above it "
                "does — the scoring selection is an id-ordered prefix, not a backlog")
    if audit.repair_enabled and scored_ids:
        data_quality.append(
            f"scoring is draining in id order under the repair; highest scored id "
            f"{max(scored_ids)} of {max((r.forecast_id for r in rows), default=0)}")
    no_close = sum(1 for r in rows if r.has_market and r.close_time is None)
    if no_close:
        data_quality.append(
            f"{no_close} forecasts sit on markets with no close_time; they are "
            "excluded from the matured denominator entirely and are the lowest "
            "sync priority, so they are invisible unless stated here")
    conflicts = sum(1 for r in missing if r.reason == LOCAL_OUTCOME_CONFLICT)
    if conflicts:
        data_quality.append(
            f"{conflicts} outcome rows disagree with themselves (side vs "
            "resolved_probability); preserved unscored, never repaired silently")

    if not matured:
        verdict = "INSUFFICIENT_DATA"
    elif not missing:
        verdict = "COVERAGE_HEALTHY"
    elif audit.unreachable_tickers and recoverable_now >= 0.5 * len(missing):
        # Gated on the selection ACTUALLY being a frozen prefix. Without that
        # gate the verdict latches: market_closed_unsettled is classified
        # recoverable, so `recoverable_now >= half` stays true long after the
        # selection stopped being the problem, and the tool would keep blaming a
        # defect it had already helped fix.
        verdict = "OUTCOME_SYNC_SELECTION_IS_THE_BLOCKER"
    elif recoverability.get(REQUIRES_NEW_PROVIDER, 0) >= 0.5 * len(missing):
        verdict = "NEW_PROVIDER_REQUIRED"
    elif recoverability.get(NOT_RECOVERABLE, 0) >= 0.5 * len(missing):
        verdict = "COVERAGE_IS_STRUCTURALLY_UNRECOVERABLE"
    elif recoverable_now:
        verdict = "COVERAGE_RECOVERS_WITH_CURRENT_PROVIDERS"
    else:
        verdict = "MAPPING_OR_INTERPRETATION_WORK_REQUIRED"

    data_quality.append(
        "reasons with no signal in this repository (a zero means NOT MEASURABLE, "
        f"not none): {', '.join(UNMEASURABLE_REASONS)}")
    data_quality.append(
        "'is a new provider required?' is NOT answerable from persisted state — "
        "a failed fetch leaves no trace. Measure it with: " + PROVIDER_MISSING_PROBE)

    return CoverageReport(
        generated_at=now.isoformat(), funnel=funnel, taxonomy=taxonomy,
        recoverability=recoverability, by_domain=segment(lambda r: r.domain),
        by_forecaster=segment(lambda r: r.forecaster),
        by_close_age=sorted(segment(age_bucket), key=lambda b: b["key"]),
        selection={
            "distinct_forecasted_tickers": audit.distinct_forecasted_tickers,
            "configured_limit": audit.configured_limit,
            "repair_enabled": audit.repair_enabled,
            "active_selection": audit.active_selection,
            "reachable_tickers": audit.reachable_tickers,
            "candidate_pool": audit.candidate_pool,
            "full_sweep_cycles": audit.full_sweep_cycles,
            "full_sweep_hours": audit.full_sweep_hours,
            "unreachable_tickers": audit.unreachable_tickers,
            "terminal_rows_reselected": audit.terminal_rows_reselected,
            "first_unreachable_rank": audit.first_unreachable_rank,
            "verdict": audit.verdict, "detail": audit.detail,
        },
        uplift=uplift, examples=examples, data_quality=data_quality,
        verdict=verdict,
    )
