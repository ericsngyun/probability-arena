"""FORECAST-SCORABILITY-AUDIT-001 — read-only forecast-scorability diagnostics.

Explains whether Probability Arena's forecast inventory can become valid
calibration evidence: a per-forecast scorability state model, a scorability +
status funnel, cohort coverage/representativeness, temporal latency, and a
deterministic primary verdict on the binding calibration bottleneck.

Measurement and data-quality analysis only. It **reads** each forecast's latest
persisted outcome and latest persisted score and REUSES the deployed scoring
semantics (`calibration._score_target`, `calibration.latest_score_for`,
`calibration.brier_score`, `outcomes.latest_outcome_for`); it never scores, never
syncs an outcome, never writes, never calls a provider, and changes no
`CalibrationService` behavior. No EV, side, size, order, recommendation, wallet,
or trading output exists here by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ForecastScoreRecord,
    MarketForecastRecord,
    MarketOutcomeRecord,
    Market,
    MarketResearchPacket,
    MarketResolutionAssessment,
)
from app.services.calibration import (
    STATUS_PENDING,
    STATUS_SCORED,
    STATUS_UNSCORABLE,
    _score_target,
    brier_score,
)

DISCLAIMER = (
    "Forecast-scorability data-quality measurement only — never advice, EV, a "
    "side, a size, an order, a recommendation, a wallet, or a trade action. "
    "Pending and unscorable rows are NOT calibration evidence."
)

# --- forecast scorability states (deterministic precedence) ---------------------
SCORED_CURRENT = "scored_current"
PENDING_NO_OUTCOME = "pending_no_outcome"
PENDING_MARKET_OPEN = "pending_market_open"
PENDING_MARKET_CLOSED_UNSETTLED = "pending_market_closed_unsettled"
UNSCORABLE_CANCELED = "unscorable_canceled"
UNSCORABLE_UNKNOWN = "unscorable_unknown"
UNSCORABLE_VOID_OR_MISSING_WINNER = "unscorable_void_or_missing_winner"
SCORABLE_SCORE_MISSING = "scorable_score_missing"
SCORABLE_SCORE_STALE = "scorable_score_stale"
PENDING_SCORE_STALE = "pending_score_stale"
UNSCORABLE_SCORE_STALE = "unscorable_score_stale"
STATE_INCONSISTENT = "state_inconsistent"

KNOWN_OUTCOME_STATUS = frozenset({"open", "closed", "settled", "canceled", "unknown"})
KNOWN_SCORE_STATUS = frozenset({STATUS_SCORED, STATUS_PENDING, STATUS_UNSCORABLE})
KNOWN_WINNING_SIDE = frozenset({"yes", "no", "void", "unknown"})

# status partition groups (mutually exclusive, exhaustive over all states)
GROUP_SCORED = "currently_scored"
GROUP_PENDING = "legitimately_pending"
GROUP_UNSCORABLE = "unscorable"
GROUP_SCORABLE_BACKLOG = "scorable_backlog"
GROUP_STALE_BACKLOG = "stale_score_backlog"
GROUP_INCONSISTENT = "inconsistent"

_STATE_GROUP = {
    SCORED_CURRENT: GROUP_SCORED,
    PENDING_NO_OUTCOME: GROUP_PENDING,
    PENDING_MARKET_OPEN: GROUP_PENDING,
    PENDING_MARKET_CLOSED_UNSETTLED: GROUP_PENDING,
    UNSCORABLE_CANCELED: GROUP_UNSCORABLE,
    UNSCORABLE_UNKNOWN: GROUP_UNSCORABLE,
    UNSCORABLE_VOID_OR_MISSING_WINNER: GROUP_UNSCORABLE,
    SCORABLE_SCORE_MISSING: GROUP_SCORABLE_BACKLOG,
    SCORABLE_SCORE_STALE: GROUP_SCORABLE_BACKLOG,
    PENDING_SCORE_STALE: GROUP_STALE_BACKLOG,
    UNSCORABLE_SCORE_STALE: GROUP_STALE_BACKLOG,
    STATE_INCONSISTENT: GROUP_INCONSISTENT,
}


def state_group(state: str) -> str:
    return _STATE_GROUP.get(state, GROUP_INCONSISTENT)


# --- verdict thresholds (conservative, measurement-only; documented constants) --
MIN_TOTAL_FORECASTS = 20        # below this the window is INSUFFICIENT_DATA
MIN_SCORED_FOR_REP = 20         # below this, representation labels are too_thin
MIN_COHORT_SCORED = 5           # per-cohort thin-sample floor
T_NO_OUTCOME = 0.20             # matured-eligible forecasts lacking any outcome row
T_CLOSED_UNSETTLED = 0.20       # matured-eligible forecasts closed-but-unsettled
T_SCORING_BACKLOG = 0.10        # settled forecasts not currently scored
T_STALE_SCORE = 0.05            # stale-score + inconsistent share
T_UNSCORABLE = 0.35             # unscorable outcome share
T_IMMATURE_ELIGIBLE = 0.25      # if matured-eligible share below this -> immature
T_HEALTHY_SCORED = 0.60         # scored share of matured-eligible for HEALTHY
REP_STRONG_PP = 15.0            # |delta| pp for strong over/under-representation
REP_MODERATE_PP = 5.0           # |delta| pp for moderate

VERDICT_HEALTHY = "HEALTHY_SCORABILITY_PIPELINE"
VERDICT_IMMATURE = "FORECAST_INVENTORY_TOO_IMMATURE"
VERDICT_OUTCOME_SYNC = "OUTCOME_SYNC_COVERAGE_IS_THE_BLOCKER"
VERDICT_SETTLEMENT = "SETTLEMENT_LATENCY_IS_THE_BLOCKER"
VERDICT_BACKLOG = "SCORING_BACKLOG_IS_THE_BLOCKER"
VERDICT_STALE = "STALE_SCORE_STATE_IS_THE_BLOCKER"
VERDICT_UNSCORABLE = "UNSCORABLE_OUTCOME_RATE_IS_THE_BLOCKER"
VERDICT_NOT_REPRESENTATIVE = "SCORED_SAMPLE_IS_NOT_REPRESENTATIVE"
VERDICT_MULTIPLE = "MULTIPLE_SCORABILITY_BLOCKERS"
VERDICT_INSUFFICIENT = "INSUFFICIENT_DATA"


# --- pure classification --------------------------------------------------------


def _score_value_matches(score: ForecastScoreRecord, probability: float, y: float | None) -> bool:
    """For a settled (scored-expected) forecast, the persisted scored row is
    CURRENT only if its stored Brier equals the Brier recomputed against the
    current settled outcome — catches an un-rescored yes<->no flip on the
    in-place outcome row."""
    if y is None or score.brier_score is None:
        return False
    return score.brier_score == brier_score(probability, y)


def classify_forecast(
    forecast: MarketForecastRecord,
    outcome: MarketOutcomeRecord | None,
    score: ForecastScoreRecord | None,
) -> str:
    """Deterministic scorability state from a forecast's latest outcome + latest
    score, reusing calibration's expected-state semantics. Never mutates."""
    # data-quality guards first — surface inconsistency, never normalize it
    if outcome is not None and outcome.outcome_status not in KNOWN_OUTCOME_STATUS:
        return STATE_INCONSISTENT
    if score is not None and score.score_status not in KNOWN_SCORE_STATUS:
        return STATE_INCONSISTENT
    if (outcome is not None and outcome.outcome_status == "settled"
            and outcome.winning_side is not None
            and outcome.winning_side not in KNOWN_WINNING_SIDE):
        return STATE_INCONSISTENT

    expected, y, _ = _score_target(outcome)  # scored | pending_outcome | unscorable
    persisted = score.score_status if score is not None else None

    if expected == STATUS_SCORED:  # outcome settled yes/no -> immediately scorable
        if persisted is None:
            return SCORABLE_SCORE_MISSING
        if persisted == STATUS_SCORED and _score_value_matches(
            score, forecast.estimated_probability, y
        ):
            return SCORED_CURRENT
        return SCORABLE_SCORE_STALE  # scored-but-stale-value, or pending/unscorable on settled

    if expected == STATUS_PENDING:  # no outcome / open / closed-unsettled
        if persisted in (None, STATUS_PENDING):
            if outcome is None:
                return PENDING_NO_OUTCOME
            return PENDING_MARKET_OPEN if outcome.outcome_status == "open" \
                else PENDING_MARKET_CLOSED_UNSETTLED
        return PENDING_SCORE_STALE  # persisted scored/unscorable but market not settled

    # expected == unscorable (canceled / unknown / settled void-or-missing-winner)
    if persisted in (None, STATUS_UNSCORABLE):
        if outcome is None:  # defensive; _score_target never returns unscorable for None
            return STATE_INCONSISTENT
        if outcome.outcome_status == "canceled":
            return UNSCORABLE_CANCELED
        if outcome.outcome_status == "unknown":
            return UNSCORABLE_UNKNOWN
        return UNSCORABLE_VOID_OR_MISSING_WINNER
    return UNSCORABLE_SCORE_STALE  # persisted scored/pending but outcome unscorable


# --- row assembly (read-only bulk loads) ----------------------------------------


@dataclass
class _Row:
    forecast: MarketForecastRecord
    outcome: MarketOutcomeRecord | None
    score: ForecastScoreRecord | None
    domain: str
    forecaster: str
    evidence_depth: str
    forecast_risk: str
    research_completeness: float | None
    research_risk: str
    resolution_risk: str
    tradeability: str
    has_market: bool
    close_time: datetime | None
    state: str


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _completeness_bucket(value: float | None) -> str:
    if value is None:
        return "missing_packet"
    if value < 0.50:
        return "0.00-0.49"
    if value < 0.75:
        return "0.50-0.74"
    if value < 0.90:
        return "0.75-0.89"
    return "0.90-1.00"


def _age_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    days = seconds / 86400.0
    if days < 1:
        return "<1d"
    if days < 3:
        return "1-3d"
    if days < 7:
        return "3-7d"
    if days < 14:
        return "7-14d"
    if days < 30:
        return "14-30d"
    return ">=30d"


def _load_rows(
    session: Session, *, now: datetime, since: datetime | None, until: datetime,
    domain: str | None, forecaster: str | None,
) -> tuple[list[_Row], int]:
    q = select(MarketForecastRecord).where(MarketForecastRecord.created_at <= until)
    if since is not None:
        q = q.where(MarketForecastRecord.created_at >= since)
    if forecaster is not None:
        q = q.where(MarketForecastRecord.forecaster_name == forecaster)
    forecasts = list(session.execute(q.order_by(MarketForecastRecord.id)).scalars().all())

    tickers = {f.market_ticker for f in forecasts}
    fids = [f.id for f in forecasts]
    packet_ids = {f.research_packet_id for f in forecasts if f.research_packet_id}
    res_ids = {f.resolution_assessment_id for f in forecasts if f.resolution_assessment_id}

    outcomes = {o.market_ticker: o for o in session.execute(
        select(MarketOutcomeRecord).where(MarketOutcomeRecord.market_ticker.in_(tickers or {""}))
    ).scalars()}
    packets = {p.id: p for p in session.execute(
        select(MarketResearchPacket).where(MarketResearchPacket.id.in_(packet_ids or {0}))
    ).scalars()}
    resolutions = {r.id: r for r in session.execute(
        select(MarketResolutionAssessment).where(MarketResolutionAssessment.id.in_(res_ids or {0}))
    ).scalars()}
    markets = {m.ticker: m for m in session.execute(
        select(Market).where(Market.ticker.in_(tickers or {""}))
    ).scalars()}

    # latest score per forecast_id (append-only history -> keep max id). This bulk
    # ascending-order last-write-wins intentionally mirrors calibration.latest_score_for
    # (order_by(id.desc()).first() == max id); kept as a bulk load only to avoid N+1.
    latest_score: dict[int, ForecastScoreRecord] = {}
    for s in session.execute(
        select(ForecastScoreRecord)
        .where(ForecastScoreRecord.forecast_id.in_(fids or {0}))
        .order_by(ForecastScoreRecord.id)
    ).scalars():
        latest_score[s.forecast_id] = s  # ascending id -> last write wins = max id

    rows: list[_Row] = []
    for f in forecasts:
        packet = packets.get(f.research_packet_id) if f.research_packet_id else None
        res = resolutions.get(f.resolution_assessment_id) if f.resolution_assessment_id else None
        market = markets.get(f.market_ticker)
        outcome = outcomes.get(f.market_ticker)
        score = latest_score.get(f.id)
        close_time = _aware(outcome.close_time if outcome and outcome.close_time
                            else (market.close_time if market else None))
        row = _Row(
            forecast=f, outcome=outcome, score=score,
            domain=(packet.domain if packet else "missing_packet"),
            forecaster=f"{f.forecaster_name}:{f.forecaster_version}",
            evidence_depth=f.evidence_depth, forecast_risk=f.forecast_risk,
            research_completeness=(packet.research_completeness_score if packet else None),
            research_risk=(packet.research_risk if packet else "missing_packet"),
            resolution_risk=(res.resolution_risk if res else "missing_assessment"),
            tradeability=(res.tradeability if res else "missing_assessment"),
            has_market=market is not None, close_time=close_time,
            state=classify_forecast(f, outcome, score),
        )
        if domain is not None and row.domain != domain:
            continue
        rows.append(row)
    dropped = len(forecasts) - len(rows) if domain is not None else 0
    return rows, dropped


# --- aggregation helpers --------------------------------------------------------


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    idx = min(len(sorted_vals) - 1, int(q * len(sorted_vals)))
    return round(sorted_vals[idx], 1)


def _group_counts(rows: list[_Row]) -> dict[str, int]:
    counts = {g: 0 for g in (GROUP_SCORED, GROUP_PENDING, GROUP_UNSCORABLE,
                             GROUP_SCORABLE_BACKLOG, GROUP_STALE_BACKLOG, GROUP_INCONSISTENT)}
    for r in rows:
        counts[state_group(r.state)] += 1
    return counts


def _cohort_stats(rows: list[_Row], all_scored: int) -> dict:
    g = _group_counts(rows)
    total = len(rows)
    scored = g[GROUP_SCORED]
    return {
        "total": total,
        "scored_current": scored,
        "legitimate_pending": g[GROUP_PENDING],
        "unscorable": g[GROUP_UNSCORABLE],
        "scorable_backlog": g[GROUP_SCORABLE_BACKLOG],
        "stale_score_backlog": g[GROUP_STALE_BACKLOG],
        "inconsistent": g[GROUP_INCONSISTENT],
        "scorability_rate": round(scored / total, 3) if total else 0.0,
        "concentration_share_of_scored": round(scored / all_scored, 3) if all_scored else 0.0,
        "sample_label": "too_thin" if scored < MIN_COHORT_SCORED else "measured",
    }


def _segment(rows: list[_Row], key, all_scored: int, limit: int) -> list[dict]:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    out = [{"name": str(name), **_cohort_stats(rs, all_scored)} for name, rs in groups.items()]
    out.sort(key=lambda d: (-d["total"], d["name"]))
    return out[:limit]


def _representation(rows: list[_Row], scored_rows: list[_Row], key, limit: int) -> list[dict]:
    n_all, n_scored = len(rows), len(scored_rows)
    all_c: dict = {}
    sc_c: dict = {}
    for r in rows:
        all_c[key(r)] = all_c.get(key(r), 0) + 1
    for r in scored_rows:
        sc_c[key(r)] = sc_c.get(key(r), 0) + 1
    out = []
    for name in sorted(all_c, key=lambda k: -all_c[k]):
        share_all = 100.0 * all_c[name] / n_all if n_all else 0.0
        share_scored = 100.0 * sc_c.get(name, 0) / n_scored if n_scored else 0.0
        delta = round(share_scored - share_all, 2)
        if sc_c.get(name, 0) < MIN_COHORT_SCORED or n_scored < MIN_SCORED_FOR_REP:
            label = "too_thin"
        elif abs(delta) < REP_MODERATE_PP:
            label = "roughly_representative"
        elif abs(delta) < REP_STRONG_PP:
            label = "moderately_overrepresented" if delta > 0 else "moderately_underrepresented"
        else:
            label = "strongly_overrepresented" if delta > 0 else "strongly_underrepresented"
        out.append({
            "name": str(name), "count_all": all_c[name], "count_scored": sc_c.get(name, 0),
            "share_all_pct": round(share_all, 2), "share_scored_pct": round(share_scored, 2),
            "representation_delta_pp": delta, "label": label,
        })
    return out[:limit]


def _latency(rows: list[_Row], now: datetime) -> dict:
    def series(fn):
        vals, missing, negatives = [], 0, 0
        for r in rows:
            v = fn(r)
            if v is None:
                missing += 1
                continue
            secs = v.total_seconds()
            if secs < 0:
                negatives += 1
            vals.append(secs)
        vals.sort()
        return {
            "count": len(vals), "missing": missing, "negative_findings": negatives,
            "median_s": _quantile(vals, 0.5), "p75_s": _quantile(vals, 0.75),
            "p90_s": _quantile(vals, 0.90), "max_s": round(vals[-1], 1) if vals else None,
        }

    def created(r):
        return _aware(r.forecast.created_at)

    def settled(r):
        return _aware(r.outcome.settled_time) if r.outcome else None

    def close(r):
        return r.close_time

    def cur_score(r):
        return _aware(r.score.created_at) if (r.score and r.state == SCORED_CURRENT) else None

    return {
        "creation_to_close": series(lambda r: close(r) - created(r) if close(r) else None),
        "creation_to_settlement": series(lambda r: settled(r) - created(r) if settled(r) else None),
        "close_to_settlement": series(
            lambda r: settled(r) - close(r) if (settled(r) and close(r)) else None),
        "settlement_to_score": series(
            lambda r: cur_score(r) - settled(r) if (cur_score(r) and settled(r)) else None),
        "creation_to_score": series(lambda r: cur_score(r) - created(r) if cur_score(r) else None),
    }


def _funnel(rows: list[_Row]) -> dict:
    """Strict monotonic subset chain: each step is conditioned on (a subset of) the
    previous step, so counts only ever attrit down. Independent per-dimension totals
    live in `counts`/`state_histogram`; this is the drawn calibration-readiness chain."""
    n = len(rows)
    has_market = [r for r in rows if r.has_market]
    has_packet = [r for r in has_market if r.forecast.research_packet_id is not None]
    has_res = [r for r in has_packet if r.forecast.resolution_assessment_id is not None]
    has_outcome = [r for r in has_res if r.outcome is not None]
    settled_yesno = [r for r in has_outcome
                     if r.outcome.outcome_status == "settled" and r.outcome.winning_side in ("yes", "no")]
    has_score = [r for r in settled_yesno if r.score is not None]
    score_current = [r for r in has_score if r.state == SCORED_CURRENT]

    def step(name, k):
        return {"step": name, "count": k, "pct_of_all": _pct(k, n)}

    return {
        "denominator": n,
        "scorability_steps": [
            step("all_forecasts", n),
            step("has_local_market_metadata", len(has_market)),
            step("has_research_packet", len(has_packet)),
            step("has_resolution_assessment", len(has_res)),
            step("has_local_outcome_row", len(has_outcome)),
            step("outcome_settled_yes_no", len(settled_yesno)),
            step("latest_score_exists", len(has_score)),
            step("valid_scored_calibration_row", len(score_current)),
        ],
    }


def build_scorability_report(
    session: Session, *, now: datetime | None = None, since: datetime | None = None,
    until: datetime | None = None, hours: int | None = None, domain: str | None = None,
    forecaster: str | None = None, top: int = 10,
) -> dict:
    """Assemble the full read-only scorability audit. Zero external calls, zero
    writes. Window: [since (or now-hours), until (or now)] over forecast.created_at."""
    now = _aware(now) or datetime.now(timezone.utc)
    until = _aware(until) or now
    if since is None and hours is not None:
        since = until - timedelta(hours=hours)
    since = _aware(since)
    if since is not None and since > until:
        raise ValueError("invalid window: since is after until")

    rows, domain_dropped = _load_rows(
        session, now=now, since=since, until=until, domain=domain, forecaster=forecaster)
    n = len(rows)
    groups = _group_counts(rows)
    scored_rows = [r for r in rows if r.state == SCORED_CURRENT]
    all_scored = len(scored_rows)

    # per-state histogram
    state_hist: dict[str, int] = {}
    for r in rows:
        state_hist[r.state] = state_hist.get(r.state, 0) + 1

    # matured-eligible = market has closed by `now`
    eligible = [r for r in rows if r.close_time is not None and r.close_time < now]

    cohorts = {
        "domain": _segment(rows, lambda r: r.domain, all_scored, top),
        "forecaster": _segment(rows, lambda r: r.forecaster, all_scored, top),
        "evidence_depth": _segment(rows, lambda r: r.evidence_depth, all_scored, top),
        "forecast_risk": _segment(rows, lambda r: r.forecast_risk, all_scored, top),
        "research_completeness": _segment(
            rows, lambda r: _completeness_bucket(r.research_completeness), all_scored, top),
        "research_risk": _segment(rows, lambda r: r.research_risk, all_scored, top),
        "resolution_risk": _segment(rows, lambda r: r.resolution_risk, all_scored, top),
        "tradeability": _segment(rows, lambda r: r.tradeability, all_scored, top),
        "forecast_age": _segment(
            rows, lambda r: _age_bucket(
                (now - _aware(r.forecast.created_at)).total_seconds()), all_scored, top),
    }
    representation = {
        "domain": _representation(rows, scored_rows, lambda r: r.domain, top),
        "evidence_depth": _representation(rows, scored_rows, lambda r: r.evidence_depth, top),
        "forecaster": _representation(rows, scored_rows, lambda r: r.forecaster, top),
        "forecast_risk": _representation(rows, scored_rows, lambda r: r.forecast_risk, top),
        "research_completeness": _representation(
            rows, scored_rows, lambda r: _completeness_bucket(r.research_completeness), top),
    }
    examples = _examples(rows, now, top)
    verdict = _verdict(n, groups, eligible, scored_rows, representation, state_hist)

    return {
        "status": "ok",
        "disclaimer": DISCLAIMER,
        "external_calls": 0,
        "persisted": False,
        "writes": 0,
        "generated_at": now.isoformat(),
        "window": {
            "since_utc": since.isoformat() if since else None,
            "until_utc": until.isoformat(),
            "hours": hours, "domain_filter": domain, "forecaster_filter": forecaster,
            "domain_filtered_out": domain_dropped,
        },
        "counts": {
            "forecasts": n, "matured_eligible": len(eligible), "scored_current": all_scored,
            **groups,
        },
        "state_histogram": dict(sorted(state_hist.items(), key=lambda x: -x[1])),
        "scorability_funnel": _funnel(rows),
        "cohorts": cohorts,
        "representation": representation,
        "latency": _latency(rows, now),
        "examples": examples,
        "verdict": verdict,
    }


def _examples(rows: list[_Row], now: datetime, top: int) -> dict:
    def sample(pred):
        return [
            {"forecast_id": r.forecast.id, "ticker": r.forecast.market_ticker,
             "state": r.state,
             "outcome_status": (r.outcome.outcome_status if r.outcome else None),
             "winning_side": (r.outcome.winning_side if r.outcome else None),
             "score_status": (r.score.score_status if r.score else None)}
            for r in rows if pred(r)
        ][:top]

    def past_close_unsettled(r):
        return (r.close_time is not None and r.close_time < now
                and not (r.outcome and r.outcome.outcome_status in ("settled", "canceled", "unknown")))

    def impossible_ts(r):
        c = _aware(r.forecast.created_at)
        s = _aware(r.outcome.settled_time) if r.outcome else None
        return bool(s and c and s < c) or bool(
            r.close_time and s and s < r.close_time - timedelta(seconds=1))

    return {
        "settled_no_score": sample(lambda r: r.state == SCORABLE_SCORE_MISSING),
        "settled_stale_score": sample(lambda r: r.state == SCORABLE_SCORE_STALE),
        "pending_score_stale": sample(lambda r: r.state == PENDING_SCORE_STALE),
        "unscorable_score_stale": sample(lambda r: r.state == UNSCORABLE_SCORE_STALE),
        "state_inconsistent": sample(lambda r: r.state == STATE_INCONSISTENT),
        "past_close_no_settlement": sample(past_close_unsettled),
        "impossible_timestamps": sample(impossible_ts),
    }


def _verdict(
    n: int, groups: dict, eligible: list[_Row], scored_rows: list[_Row],
    representation: dict, state_hist: dict,
) -> dict:
    if n < MIN_TOTAL_FORECASTS:
        return {"primary": VERDICT_INSUFFICIENT, "blockers": [],
                "reason": f"only {n} forecasts in window (<{MIN_TOTAL_FORECASTS})", "rates": {}}

    n_elig = len(eligible)
    eg = _group_counts(eligible)
    # rates over matured-eligible forecasts (the population that COULD be scored)
    def er(k):
        return round(k / n_elig, 3) if n_elig else 0.0

    elig_no_outcome = sum(1 for r in eligible if r.outcome is None
                          or r.outcome.outcome_status == "open")
    elig_closed_unsettled = sum(
        1 for r in eligible if r.outcome and r.outcome.outcome_status == "closed")
    settled_scored = eg[GROUP_SCORED]
    scorable_backlog = eg[GROUP_SCORABLE_BACKLOG]
    stale = eg[GROUP_STALE_BACKLOG] + eg[GROUP_INCONSISTENT]
    unscorable = eg[GROUP_UNSCORABLE]

    rates = {
        "matured_eligible_share": round(n_elig / n, 3),
        "eligible_no_outcome_rate": er(elig_no_outcome),
        "eligible_closed_unsettled_rate": er(elig_closed_unsettled),
        "eligible_scorable_backlog_rate": er(scorable_backlog),
        "eligible_stale_or_inconsistent_rate": er(stale),
        "eligible_unscorable_rate": er(unscorable),
        "eligible_scored_rate": er(settled_scored),
    }

    # immaturity: too few forecasts' markets have closed yet
    if rates["matured_eligible_share"] < T_IMMATURE_ELIGIBLE:
        return {"primary": VERDICT_IMMATURE, "blockers": [],
                "reason": f"only {rates['matured_eligible_share']:.0%} of forecasts' markets "
                          f"have closed; inventory not yet matured", "rates": rates}

    blockers = []
    if rates["eligible_stale_or_inconsistent_rate"] >= T_STALE_SCORE:
        blockers.append(VERDICT_STALE)
    if rates["eligible_scorable_backlog_rate"] >= T_SCORING_BACKLOG:
        blockers.append(VERDICT_BACKLOG)
    if rates["eligible_no_outcome_rate"] >= T_NO_OUTCOME:
        blockers.append(VERDICT_OUTCOME_SYNC)
    if rates["eligible_closed_unsettled_rate"] >= T_CLOSED_UNSETTLED:
        blockers.append(VERDICT_SETTLEMENT)
    if rates["eligible_unscorable_rate"] >= T_UNSCORABLE:
        blockers.append(VERDICT_UNSCORABLE)

    strong_skew = any(
        c["label"].startswith("strongly_") for dim in representation.values() for c in dim)

    if len(blockers) >= 2:
        primary = VERDICT_MULTIPLE
    elif len(blockers) == 1:
        primary = blockers[0]
    elif rates["eligible_scored_rate"] >= T_HEALTHY_SCORED:
        primary = VERDICT_NOT_REPRESENTATIVE if (
            strong_skew and len(scored_rows) >= MIN_SCORED_FOR_REP) else VERDICT_HEALTHY
    elif strong_skew and len(scored_rows) >= MIN_SCORED_FOR_REP:
        primary = VERDICT_NOT_REPRESENTATIVE
    else:
        primary = VERDICT_INSUFFICIENT

    return {"primary": primary, "blockers": blockers, "reason": "see rates", "rates": rates}
