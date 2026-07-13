"""CRYPTO-COVERAGE-001 — read-only tape-coverage forensics.

Diagnoses WHY CRYPTO-TAPE survival horizons (15m/1h/6h/24h) stay
overwhelmingly unmeasurable even after repeated cadence sessions. It
decomposes every unknown survival outcome into a specific, actionable cause
and asks the load-bearing question: can the current recorder token-selection
(recent-first) and revisit policy ever mature 6h/24h outcomes efficiently, or
does it structurally starve the old cohorts whose long horizons are due?

Two failure modes are deliberately separated, because they need opposite
fixes:
  * UPSTREAM TICK COVERAGE — a survival horizon only matures from
    crypto_price_ticks, which the BACKGROUND crypto scout collects, not the
    tape. If the scout stopped ticking a token near its 6h/24h mark, the
    horizon is unmeasurable no matter how many tape sessions run
    (token_inactive_or_disappeared / no_price_tick_near_horizon).
  * REVISIT / SELECTION — the recorder picks tokens recent-first, so an OLD
    token whose 6h/24h is due ranks below the limit and is never recomputed
    even when the ticks it needs already exist
    (token_not_revisited_after_due). This is fixable by selection policy and
    is estimated here via a SHADOW-ONLY comparison (the live recorder is not
    changed).

Compute-on-demand, exactly like MEME-SHADOW / CRYPTO-RETROSPECT: it PERSISTS
NOTHING, makes ZERO external calls, has ZERO provider-budget impact, adds no
migration/table/flag/timer, and changes no stored outcome label or MarketOps
behavior. It reuses the CRYPTO-TAPE recorder's pure builders.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): MEASUREMENT/diagnostic
only. Nothing here is EV, a return, a side, a size, a recommendation, or a
trade direction. No wallets, keys, swaps, signing, orders, execution, or
autonomy.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CryptoToken,
    CryptoTokenBirthEvent,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_tape import (
    HORIZON_TOLERANCE,
    HORIZONS,
    CryptoLifecycleTapeRecorder,
    _aware,
    _now,
)

logger = logging.getLogger(__name__)

COVERAGE_NOTE = (
    "Read-only tape-coverage forensics: decomposes every unmeasurable survival "
    "horizon into an actionable cause and estimates (shadow-only) whether "
    "selection/revisit policy can mature 6h/24h outcomes. Derived on demand "
    "from already-persisted rows — nothing persisted, no external call, no "
    "provider-budget impact, no live selection change. Diagnostic only — never "
    "PnL, EV, a side, a size, or a recommendation. No wallets, keys, swaps, "
    "signing, orders, or execution."
)

# explicit gap-cause categories (replace the generic gap explanation)
CAUSE_NOT_DUE = "horizon_not_due"
CAUSE_NOT_REVISITED = "token_not_revisited_after_due"
CAUSE_NO_TICK = "no_price_tick_near_horizon"
CAUSE_NO_LIQ = "no_pair_or_liquidity_state_near_horizon"
CAUSE_NO_RISK = "no_risk_snapshot_near_horizon"
CAUSE_OUTSIDE_TOL = "outside_tolerance_only"
CAUSE_JOIN_FAILED = "source_rows_exist_but_join_failed"
CAUSE_INACTIVE = "token_inactive_or_disappeared"
CAUSE_UNKNOWN = "unknown_gap_cause"

ALL_CAUSES = (
    CAUSE_NOT_DUE, CAUSE_NOT_REVISITED, CAUSE_NO_TICK, CAUSE_NO_LIQ,
    CAUSE_NO_RISK, CAUSE_OUTSIDE_TOL, CAUSE_JOIN_FAILED, CAUSE_INACTIVE,
    CAUSE_UNKNOWN,
)

# maturity status of a horizon relative to "now"
STATUS_NOT_YET_DUE = "not_yet_due"
STATUS_DUE = "due"
STATUS_OVERDUE = "overdue"

# a later tick this far past the tolerance window counts as "just outside"
# (actionable by widening tolerance) rather than a true coverage hole
OUTSIDE_TOLERANCE_MULTIPLE = 2.0

MAX_BIRTHS = 500              # manual report on a shared host
DEFAULT_WINDOW_HOURS = 168   # 7d — cover all taped births by default
SELECTION_HORIZONS = ("6h", "24h")   # the horizons that are actually starving


def _minutes(label: str) -> int:
    return dict(HORIZONS)[label]


def _nearest(rows, target: datetime, key):
    """(before, after) nearest rows to target by the `key` timestamp accessor.
    before = latest row at//before target, after = earliest row at/after."""
    before = after = None
    for row in rows:
        ts = _aware(key(row))
        if ts is None:
            continue
        if ts <= target:
            if before is None or _aware(key(before)) < ts:
                before = row
        else:
            if after is None or _aware(key(after)) > ts:
                after = row
    return before, after


@dataclass
class HorizonDiag:
    label: str
    target: datetime
    tolerance_seconds: float
    status: str                       # not_yet_due / due / overdue
    stored_known: bool
    fresh_measurable: bool            # would compute_survival mark it now?
    cause: str | None                 # None => matured (not a gap)
    nearest_tick_before_s: float | None
    nearest_tick_after_s: float | None
    nearest_snapshot_before_s: float | None
    nearest_snapshot_after_s: float | None
    nearest_risk_before_s: float | None
    nearest_risk_after_s: float | None
    within_tolerance: bool
    later_rows_exist: bool
    revisited_after_due: bool


@dataclass
class TokenCoverage:
    token_address: str
    symbol: str | None
    birth_at: datetime | None
    first_seen_rank: int              # recent-first rank (0 = newest)
    run_appearances: int              # distinct tape runs that observed it
    last_observed_at: datetime | None
    horizons: dict = field(default_factory=dict)   # label -> HorizonDiag


class CryptoCoverageService:
    """Per-token horizon forensics. Session-only; composes the tape recorder's
    pure builders; persists nothing (no row is ever added to the session)."""

    def __init__(self, recorder: CryptoLifecycleTapeRecorder | None = None):
        self.recorder = recorder or CryptoLifecycleTapeRecorder()

    def _rank_map(self, session: Session, hours: int) -> dict[str, int]:
        """recent-first rank of every in-window token — the recorder's own
        selection order (first_seen_at desc)."""
        cutoff = _now() - timedelta(hours=hours)
        tokens = session.execute(
            select(CryptoToken)
            .where(
                CryptoToken.chain == self.recorder.config.chain,
                CryptoToken.first_seen_at >= cutoff,
            )
            .order_by(CryptoToken.first_seen_at.desc(), CryptoToken.id.desc())
        ).scalars().all()
        return {t.token_address: i for i, t in enumerate(tokens)}

    def token_coverages(
        self, session: Session, hours: int = DEFAULT_WINDOW_HOURS
    ) -> list[TokenCoverage]:
        now = _now()
        cutoff = now - timedelta(hours=hours)
        births = list(session.execute(
            select(CryptoTokenBirthEvent)
            .where(
                CryptoTokenBirthEvent.chain == self.recorder.config.chain,
                CryptoTokenBirthEvent.observed_at >= cutoff,
            )
            .order_by(CryptoTokenBirthEvent.id.desc())
            .limit(MAX_BIRTHS)
        ).scalars().all())
        if not births:
            return []

        addresses = [b.token_address for b in births]
        tokens = {
            t.token_address: t
            for t in session.execute(
                select(CryptoToken).where(CryptoToken.token_address.in_(addresses))
            ).scalars().all()
        }
        outcomes = {
            o.birth_event_id: o
            for o in session.execute(
                select(CryptoTokenSurvivalOutcome).where(
                    CryptoTokenSurvivalOutcome.birth_event_id.in_([b.id for b in births])
                )
            ).scalars().all()
        }
        # snapshot observation times per token (revisit history)
        snap_rows = session.execute(
            select(
                CryptoTokenLifecycleSnapshot.token_address,
                CryptoTokenLifecycleSnapshot.observed_at,
            ).where(CryptoTokenLifecycleSnapshot.token_address.in_(addresses))
        ).all()
        snaps_by_token: dict[str, list[datetime]] = {}
        for addr, observed_at in snap_rows:
            snaps_by_token.setdefault(addr, []).append(_aware(observed_at))

        ranks = self._rank_map(session, hours)
        results: list[TokenCoverage] = []
        for birth in births:
            token = tokens.get(birth.token_address)
            if token is None:
                continue
            sources = self.recorder._load_sources(session, token, now)
            fresh = self.recorder.compute_survival(birth, sources, now)
            stored = outcomes.get(birth.id)
            snap_times = sorted(t for t in snaps_by_token.get(birth.token_address, []) if t)
            anchor = _aware(birth.first_evidence_at)

            tc = TokenCoverage(
                token_address=birth.token_address,
                symbol=birth.symbol,
                birth_at=anchor,
                first_seen_rank=ranks.get(birth.token_address, len(ranks)),
                run_appearances=len(snap_times),
                last_observed_at=(snap_times[-1] if snap_times else None),
            )
            for label, minutes in HORIZONS:
                tc.horizons[label] = self._horizon_diag(
                    birth, sources, fresh, stored, snap_times, label, minutes, now,
                )
            results.append(tc)
        return results

    def _horizon_diag(
        self, birth, sources, fresh, stored, snap_times, label, minutes, now,
    ) -> HorizonDiag:
        anchor = _aware(birth.first_evidence_at)
        # anchor is guaranteed by build_birth_event for taped tokens; guard anyway
        target = (anchor + timedelta(minutes=minutes)) if anchor else now
        tol = timedelta(minutes=minutes * HORIZON_TOLERANCE)

        # maturity status
        if anchor is None or now < target - tol:
            status = STATUS_NOT_YET_DUE
        elif now > target + tol:
            status = STATUS_OVERDUE
        else:
            status = STATUS_DUE

        key = f"survived_{label}"
        stored_known = bool(stored is not None and getattr(stored, key) is not None)
        fresh_measurable = fresh["labels"].get(key) is not None

        later = [
            t for t in sources.ticks
            if anchor is not None and _aware(t.observed_at) is not None
            and _aware(t.observed_at) > anchor
        ]
        in_window = [
            t for t in later if abs(_aware(t.observed_at) - target) <= tol
        ]
        # nearest raw rows around the target (forensics)
        tb, ta = _nearest(sources.ticks, target, lambda t: t.observed_at)
        sb, sa = _nearest(
            [type("S", (), {"observed_at": t})() for t in snap_times],
            target, lambda s: s.observed_at,
        )
        rb, ra = _nearest(sources.assessments, target, lambda a: a.created_at)

        def secs(row, key_fn):
            if row is None:
                return None
            ts = _aware(key_fn(row))
            return round((ts - target).total_seconds(), 1) if ts else None

        revisited_after_due = any(t >= target - tol for t in snap_times)
        nearest_after_dist = (
            min((abs((_aware(t.observed_at) - target).total_seconds()) for t in later),
                default=None)
        )

        cause = self._classify(
            status=status, stored_known=stored_known,
            fresh_measurable=fresh_measurable, later=later, in_window=in_window,
            nearest_after_dist=nearest_after_dist, tol=tol,
            revisited_after_due=revisited_after_due, stored=stored,
        )
        return HorizonDiag(
            label=label, target=target, tolerance_seconds=tol.total_seconds(),
            status=status, stored_known=stored_known,
            fresh_measurable=fresh_measurable, cause=cause,
            nearest_tick_before_s=secs(tb, lambda t: t.observed_at),
            nearest_tick_after_s=secs(ta, lambda t: t.observed_at),
            nearest_snapshot_before_s=secs(sb, lambda s: s.observed_at),
            nearest_snapshot_after_s=secs(sa, lambda s: s.observed_at),
            nearest_risk_before_s=secs(rb, lambda a: a.created_at),
            nearest_risk_after_s=secs(ra, lambda a: a.created_at),
            within_tolerance=bool(in_window),
            later_rows_exist=bool(later),
            revisited_after_due=revisited_after_due,
        )

    @staticmethod
    def _classify(
        status, stored_known, fresh_measurable, later, in_window,
        nearest_after_dist, tol, revisited_after_due, stored,
    ) -> str | None:
        """The explicit gap cause for one horizon (None => matured)."""
        if stored_known:
            return None  # already matured in the persisted outcome — a success
        if status == STATUS_NOT_YET_DUE:
            return CAUSE_NOT_DUE
        # due / overdue and stored is unknown
        if fresh_measurable:
            # the raw rows needed exist NOW; why is the stored outcome unknown?
            if revisited_after_due:
                # a tape run re-observed the token after due but did not capture
                # the measurable outcome — a genuine join/recompute failure
                return CAUSE_JOIN_FAILED
            # never recomputed after the data arrived — fixable by revisiting
            return CAUSE_NOT_REVISITED
        # fresh not measurable: an upstream data gap
        if not later:
            return CAUSE_INACTIVE
        if in_window:
            # a tick sits in the window but lacks liquidity state
            return CAUSE_NO_LIQ
        if nearest_after_dist is not None and nearest_after_dist <= OUTSIDE_TOLERANCE_MULTIPLE * tol.total_seconds():
            return CAUSE_OUTSIDE_TOL
        if later:
            return CAUSE_NO_TICK
        return CAUSE_UNKNOWN


# --- aggregation: funnel, causes, selection, shadow, examples -------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def coverage_funnel(coverages: list[TokenCoverage], label: str) -> dict:
    """The born -> due -> revisited -> data -> in-tolerance -> measurable ->
    provider_gap funnel for one horizon."""
    born = len(coverages)
    due = revisited = has_data = in_tol = measurable = gap = 0
    for tc in coverages:
        h = tc.horizons[label]
        if h.status in (STATUS_DUE, STATUS_OVERDUE):
            due += 1
            if h.revisited_after_due:
                revisited += 1
            if h.later_rows_exist:
                has_data += 1
            if h.within_tolerance:
                in_tol += 1
            if h.stored_known:
                measurable += 1
            else:
                gap += 1
    return {
        "horizon": label,
        "tokens_born": born,
        "horizon_due": due,
        "revisited_after_due": revisited,
        "raw_market_data_available": has_data,
        "tick_within_tolerance": in_tol,
        "outcome_measurable": measurable,
        "provider_gap": gap,
        "rates_vs_due": {
            "revisited_after_due": _rate(revisited, due),
            "raw_market_data_available": _rate(has_data, due),
            "tick_within_tolerance": _rate(in_tol, due),
            "outcome_measurable": _rate(measurable, due),
            "provider_gap": _rate(gap, due),
        },
    }


def cause_histogram(coverages: list[TokenCoverage], label: str) -> dict:
    hist = {c: 0 for c in ALL_CAUSES}
    for tc in coverages:
        cause = tc.horizons[label].cause
        if cause is not None:
            hist[cause] += 1
    return {c: n for c, n in hist.items() if n}


def selection_analysis(coverages: list[TokenCoverage], limit: int) -> dict:
    """Does recent-first selection starve the old cohorts whose long horizons
    are due? Ranks are the recorder's own first_seen-desc order."""
    appearances = [tc.run_appearances for tc in coverages]
    due_ranks: dict[str, list[int]] = {}
    omitted: dict[str, int] = {}
    for label in SELECTION_HORIZONS:
        ranks = [
            tc.first_seen_rank for tc in coverages
            if tc.horizons[label].status in (STATUS_DUE, STATUS_OVERDUE)
        ]
        due_ranks[label] = ranks
        omitted[label] = sum(1 for r in ranks if r >= limit)
    # do due tokens rank BELOW the selection cutoff? (starvation signal)
    starves = {
        label: _rate(omitted[label], len(due_ranks[label]))
        for label in SELECTION_HORIZONS
    }
    return {
        "limit": limit,
        "appearances_min": min(appearances) if appearances else None,
        "appearances_max": max(appearances) if appearances else None,
        "appearances_mean": (
            round(sum(appearances) / len(appearances), 2) if appearances else None
        ),
        "due_tokens_omitted_from_limit": omitted,
        "due_token_omission_rate": starves,
        "recent_first_starves_old_cohorts": any(
            (rate or 0) >= 0.5 for rate in starves.values()
        ),
    }


def _maturable(tc: TokenCoverage, label: str) -> bool:
    """A due horizon that WOULD newly mature if the token were recomputed now
    (raw data present, stored still unknown)."""
    h = tc.horizons[label]
    return (
        h.status in (STATUS_DUE, STATUS_OVERDUE)
        and h.fresh_measurable
        and not h.stored_known
    )


def shadow_selection(coverages: list[TokenCoverage], limit: int) -> dict:
    """SHADOW-ONLY: how many currently-unmeasured-but-maturable 6h/24h
    outcomes would each selection policy pick up on the NEXT run? The live
    recorder selection is NOT changed by this milestone."""

    def overdue_key(tc: TokenCoverage) -> float:
        # most-overdue due horizon first (largest seconds past target)
        overdue = [
            (tc.birth_at is not None)
            and (_now() - tc.horizons[label].target).total_seconds()
            for label in SELECTION_HORIZONS
            if tc.horizons[label].status in (STATUS_DUE, STATUS_OVERDUE)
        ]
        vals = [v for v in overdue if isinstance(v, (int, float))]
        return max(vals) if vals else float("-inf")

    recent_first = sorted(
        coverages, key=lambda tc: tc.first_seen_rank
    )
    due_first = sorted(coverages, key=overdue_key, reverse=True)
    oldest_cohort = sorted(
        coverages,
        key=lambda tc: (tc.birth_at or _now()),
    )  # earliest births = the fixed replay cohort

    def gain(selection: list[TokenCoverage]) -> dict:
        picked = selection[:limit]
        by_h = {
            label: sum(1 for tc in picked if _maturable(tc, label))
            for label in SELECTION_HORIZONS
        }
        return {
            "selected": len(picked),
            "expected_new_matures_by_horizon": by_h,
            "expected_new_matures_total": sum(by_h.values()),
        }

    # mixed: half recent, half due-first (deduped, preserving order)
    mixed: list[TokenCoverage] = []
    seen: set[str] = set()
    for a, b in zip(recent_first, due_first):
        for tc in (a, b):
            if tc.token_address not in seen:
                seen.add(tc.token_address)
                mixed.append(tc)

    total_maturable = {
        label: sum(1 for tc in coverages if _maturable(tc, label))
        for label in SELECTION_HORIZONS
    }
    return {
        "limit": limit,
        "total_maturable_available": total_maturable,
        "policies": {
            "current_recent_selection": gain(recent_first),
            "due_horizon_first": gain(due_first),
            "fixed_cohort_revisit": gain(oldest_cohort),
            "mixed_new_and_due": gain(mixed),
        },
    }


def coverage_examples(coverages: list[TokenCoverage], top: int) -> dict:
    """Concrete tokens per interesting cause, for eyeballing."""
    buckets: dict[str, list] = {
        "overdue_never_revisited": [],
        "revisited_missing_ticks": [],
        "ticks_outside_tolerance": [],
        "raw_data_join_failed": [],
        "successfully_matured": [],
    }
    for tc in coverages:
        for label in SELECTION_HORIZONS:
            h = tc.horizons[label]
            ex = {
                "token": tc.token_address[:16], "symbol": tc.symbol,
                "horizon": label, "cause": h.cause,
                "rank": tc.first_seen_rank, "appearances": tc.run_appearances,
            }
            if h.cause == CAUSE_NOT_REVISITED and not h.revisited_after_due:
                buckets["overdue_never_revisited"].append(ex)
            elif h.cause == CAUSE_INACTIVE:
                buckets["revisited_missing_ticks"].append(ex)
            elif h.cause == CAUSE_OUTSIDE_TOL:
                buckets["ticks_outside_tolerance"].append(ex)
            elif h.cause == CAUSE_JOIN_FAILED:
                buckets["raw_data_join_failed"].append(ex)
            elif h.cause is None and h.status in (STATUS_DUE, STATUS_OVERDUE):
                buckets["successfully_matured"].append(ex)
    return {k: v[:top] for k, v in buckets.items()}


def build_coverage_report(
    session: Session, hours: int = DEFAULT_WINDOW_HOURS, top: int = 5,
    limit: int = 25,
) -> dict:
    """Full read-only coverage-forensics report. Derived on demand."""
    service = CryptoCoverageService()
    coverages = service.token_coverages(session, hours=hours)
    now = _now()

    funnels = {label: coverage_funnel(coverages, label) for label, _ in HORIZONS}
    causes = {label: cause_histogram(coverages, label) for label, _ in HORIZONS}

    # headline verdict: is the ceiling upstream tick coverage or revisit policy?
    def cause_share(label, cause_set) -> float | None:
        hist = causes[label]
        gap_total = sum(hist.values())
        return _rate(sum(hist.get(c, 0) for c in cause_set), gap_total)

    upstream = {CAUSE_INACTIVE, CAUSE_NO_TICK, CAUSE_NO_LIQ, CAUSE_OUTSIDE_TOL}
    revisit = {CAUSE_NOT_REVISITED, CAUSE_JOIN_FAILED}
    verdict = {}
    for label in SELECTION_HORIZONS:
        up = cause_share(label, upstream) or 0
        rv = cause_share(label, revisit) or 0
        verdict[label] = {
            "upstream_coverage_share": round(up, 4),
            "revisit_policy_share": round(rv, 4),
            "bottleneck": (
                "upstream_tick_coverage" if up > rv
                else ("revisit_policy" if rv > up else "mixed_or_immature")
            ),
        }

    return {
        "note": COVERAGE_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "tokens_analyzed": len(coverages),
        "selection_limit": limit,
        "coverage_funnel": funnels,
        "gap_causes": causes,
        "bottleneck_verdict": verdict,
        "selection_analysis": selection_analysis(coverages, limit),
        "shadow_selection": shadow_selection(coverages, limit),
        "examples": coverage_examples(coverages, top),
        "disclaimer": (
            "coverage forensics only — a diagnostic decomposition of why "
            "outcomes are unmeasurable and a shadow estimate of selection "
            "policy; changes no stored label, no live selection, nothing "
            "persisted; never advice, no EV, no recommendation, no sizing, no "
            "orders, no wallets, no execution"
        ),
    }
