"""COST-MODEL-001 — read-only cost-adjusted follow-through MEASUREMENT.

Every shadow metric so far (FOLLOWTHROUGH/EDGE-FILTER/TRIGGER-TIMING/
EDGE-SELECTION) is midpoint-based and frictionless. This module asks the
missing question WITHOUT touching anything live: *after spread, a conservative
Kalshi fee assumption, and executable touch prices, does any cohort's measured
movement survive?* A cohort that looks positive at frictionless midpoints but
negative at executable prices is `cost_killed` — evidence that the apparent
edge is an artifact of measuring at prices no one can transact at.

Friction model (documented, conservative, configurable):
- half-spread: spread_cents / 2 / 100 probability points, charged once
  against the measured toward-move (crossing from midpoint to the touch).
- fee: Kalshi's published taker fee is ceil(0.07 * C * P * (1-P)) per
  contract. We charge `kalshi_fee_rate_assumption` (default 0.07) * P * (1-P)
  at BOTH the trigger and the horizon — a full round trip, no maker rebates,
  no rounding down. Conservative by construction.
- executable-touch: the toward-move is re-measured from the touch prices a
  taker would actually see: forecast above market -> from the ask at trigger
  to the bid at horizon; forecast below market -> from the bid at trigger to
  the ask at horizon (the NO-side round trip is arithmetically identical).
  Rows without usable touch quotes are counted as uncovered, never guessed.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): READ-ONLY SHADOW
MEASUREMENT. All numbers are measured market movement net of assumed friction
— they are NOT expected value, NOT profit-and-loss, NOT a recommendation, and
never advice. No fills, positions, sizing, dollar amounts, orders, wallets,
keys, signing, swaps, or automation of any kind. Nothing is persisted; no
external call; no gate/forecast/promotion/flag/MarketOps/EDGE-AUTO change. A
`promising_friction_adjusted_shadow` label motivates observation only; any
live change remains a separate explicitly-accepted milestone.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import MarketPriceTick
from app.services.edge_cohort import FOLLOW_THROUGH_HORIZONS_MINUTES
from app.services.edge_filter_shadow import (
    MAX_GAME_SHARE,
    MAX_TICKER_SHARE,
    MIN_READABLE_FINAL_N,
    POLICIES,
    game_of,
)
from app.services.edge_followthrough import (
    EdgeFollowthroughDiagnosticService,
    RowDiagnostic,
    _aware,
    _mean,
    _rate,
)
from app.services.edge_selection import (
    PREREG_LOCKED_AT,
    PREREGISTERED,
    WINDOW_VALIDATION,
    classify_window,
)

logger = logging.getLogger(__name__)

COST_NOTE = (
    "Read-only cost-adjusted SHADOW measurement. Frictionless midpoint "
    "follow-through is re-measured net of half-spread, a conservative Kalshi "
    "fee assumption (round trip, no rebates), and executable touch prices — "
    "over rows and ticks that already exist. Nothing changes live. Every "
    "number is measured market movement net of assumed friction — not EV, not "
    "PnL, never advice; labels motivate observation only. No sizing, orders, "
    "wallets, keys, swaps, signing, or execution."
)

MVP_005B_NOTE = (
    "MVP-005B remains blocked unless explicit human acceptance — regardless of "
    "any label in this report."
)

FINAL_MINUTES = FOLLOW_THROUGH_HORIZONS_MINUTES[-1]   # 60
FINAL_HORIZON = f"{FINAL_MINUTES}m"

# label ladder (COST-MODEL-001)
LABEL_TOO_THIN = "too_thin"
LABEL_COST_KILLED = "cost_killed"
LABEL_NEUTRAL = "neutral"
LABEL_PROMISING = "promising_friction_adjusted_shadow"

PROMISING_MIN_N = 75
PROMISING_TOWARD_60 = 0.55


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- pure cost math (probability points, 0-1 scale) --------------------------------


def half_spread_pts(spread_cents: int | None) -> float | None:
    if spread_cents is None or spread_cents < 0:
        return None
    return spread_cents / 2 / 100


def fee_pts(p_trigger: float, p_horizon: float, rate: float) -> float:
    """Conservative round-trip fee assumption in probability points: the
    Kalshi taker-fee shape rate*P*(1-P), charged at BOTH measurement ends."""
    return rate * (p_trigger * (1 - p_trigger) + p_horizon * (1 - p_horizon))


def toward_move_pts(m0: float, mh: float, gap: float) -> float:
    """Signed movement TOWARD the forecast in probability points: positive =
    the market moved the way the gap pointed."""
    return (mh - m0) if gap > 0 else (m0 - mh)


def touch_move_pts(
    bid0: float | None, ask0: float | None,
    bid_h: float | None, ask_h: float | None,
    gap: float,
) -> float | None:
    """Toward-movement re-measured from executable touch prices. Forecast
    above market (gap>0): from the trigger ask to the horizon bid. Forecast
    below market (gap<0): from the trigger bid to the horizon ask (identical
    to the NO-side round trip). None when either touch is missing."""
    if gap > 0:
        if ask0 is None or bid_h is None:
            return None
        return bid_h - ask0
    if bid0 is None or ask_h is None:
        return None
    return bid0 - ask_h


@dataclass
class RowCost:
    """One row's frictionless and cost-adjusted 60m measurements. Closures are
    normalized by |gap| like every other follow-through metric in the repo."""

    row: RowDiagnostic
    midpoint_trigger: float
    midpoint_horizon: float
    spread_cents_trigger: int | None
    frictionless_closure: float
    frictionless_toward: bool
    net_half_spread_closure: float | None
    fee_adjusted_closure: float | None       # net of half-spread AND fees
    touch_closure: float | None              # from executable touch prices
    touch_covered: bool


def compute_row_cost(
    row: RowDiagnostic,
    trigger_tick: MarketPriceTick | None,
    horizon_tick: MarketPriceTick | None,
    fee_rate: float,
) -> RowCost | None:
    """Cost-adjusted measurement for one row, or None when the row has no
    usable frictionless measurement (no recorded gap or no horizon tick)."""
    if (
        row.gap is None or row.forecast_probability is None
        or abs(row.gap) < 1e-9
        or horizon_tick is None or horizon_tick.midpoint is None
    ):
        return None
    m0 = round(row.forecast_probability - row.gap, 4)   # recorded trigger midpoint
    mh = horizon_tick.midpoint
    abs_gap = abs(row.gap)
    move = toward_move_pts(m0, mh, row.gap)
    frictionless = round(move / abs_gap, 4)

    spread_cents = row.spread_cents
    if spread_cents is None and trigger_tick is not None:
        spread_cents = trigger_tick.spread
    half = half_spread_pts(spread_cents)
    net_half = round((move - half) / abs_gap, 4) if half is not None else None
    fee = fee_pts(m0, mh, fee_rate)
    fee_adjusted = (
        round((move - half - fee) / abs_gap, 4) if half is not None else None
    )

    def cents(v: int | None) -> float | None:
        return v / 100 if v is not None else None

    touch = touch_move_pts(
        cents(trigger_tick.yes_bid) if trigger_tick else None,
        cents(trigger_tick.yes_ask) if trigger_tick else None,
        cents(horizon_tick.yes_bid),
        cents(horizon_tick.yes_ask),
        row.gap,
    )
    return RowCost(
        row=row,
        midpoint_trigger=m0,
        midpoint_horizon=mh,
        spread_cents_trigger=spread_cents,
        frictionless_closure=frictionless,
        frictionless_toward=frictionless > 0,
        net_half_spread_closure=net_half,
        fee_adjusted_closure=fee_adjusted,
        touch_closure=round(touch / abs_gap, 4) if touch is not None else None,
        touch_covered=touch is not None,
    )


# --- cohorts -----------------------------------------------------------------------


def _liquidity_bucket(r: RowDiagnostic) -> str:
    v = r.liquidity_cents
    if v is None:
        return "unknown"
    if v < 100_000:
        return "lt_100k_cents"
    if v < 1_000_000:
        return "100k_1m_cents"
    return "ge_1m_cents"


def _spread_bucket(r: RowDiagnostic) -> str:
    v = r.spread_cents
    if v is None:
        return "unknown"
    if v <= 1:
        return "le_1c"
    if v == 2:
        return "2c"
    if v <= 4:
        return "3_4c"
    return "ge_5c"


def _confidence_bucket(r: RowDiagnostic) -> str:
    v = r.confidence
    if v is None:
        return "unknown"
    if v < 0.55:
        return "lt_0.55"
    if v < 0.65:
        return "0.55_0.65"
    return "ge_0.65"


def summarize_cohort(name: str, dimension: str, costs: list[RowCost]) -> dict:
    n = len(costs)
    covered = [c for c in costs if c.touch_covered]
    tickers: dict[str, int] = {}
    games: dict[str, int] = {}
    types: dict[str, int] = {}
    for c in costs:
        tickers[c.row.market_ticker] = tickers.get(c.row.market_ticker, 0) + 1
        g = game_of(c.row.market_ticker)
        games[g] = games.get(g, 0) + 1
        types[c.row.market_type] = types.get(c.row.market_type, 0) + 1
    return {
        "name": name,
        "dimension": dimension,
        "final_n": n,
        "touch_coverage": _rate(len(covered), n),
        "toward_rate_60m": _rate(sum(1 for c in costs if c.frictionless_toward), n),
        "frictionless_closure_60m": _mean([c.frictionless_closure for c in costs]),
        "net_closure_after_half_spread_60m": _mean(
            [c.net_half_spread_closure for c in costs]
        ),
        "fee_adjusted_net_closure_60m": _mean([c.fee_adjusted_closure for c in costs]),
        "executable_touch_closure_60m": _mean([c.touch_closure for c in costs]),
        "market_type_mix": dict(sorted(types.items(), key=lambda kv: -kv[1])),
        "max_ticker_share": _rate(max(tickers.values()), n) if tickers else None,
        "max_game_share": _rate(max(games.values()), n) if games else None,
    }


def label_cohort(summary: dict, window_type: str) -> tuple[str, str]:
    """Conservative deterministic label. Order: too_thin (unreadable) ->
    promising_friction_adjusted_shadow (all bars, incl. out-of-sample when the
    cohort is a pre-registered policy) -> cost_killed (positive frictionless,
    non-positive after friction) -> neutral. Labels are measurement quality —
    never advice, never an authorization."""
    n = summary["final_n"]
    toward = summary["toward_rate_60m"]
    frictionless = summary["frictionless_closure_60m"]
    fee_adj = summary["fee_adjusted_net_closure_60m"]
    touch = summary["executable_touch_closure_60m"]
    if n < MIN_READABLE_FINAL_N:
        return LABEL_TOO_THIN, f"final_n={n} < {MIN_READABLE_FINAL_N}"

    concentration_ok = (
        (summary["max_ticker_share"] or 0) <= MAX_TICKER_SHARE
        and (summary["max_game_share"] or 0) <= MAX_GAME_SHARE
    )
    needs_oos = summary["dimension"] == "preregistered_policy"
    if (
        n >= PROMISING_MIN_N
        and touch is not None and touch > 0
        and fee_adj is not None and fee_adj > 0
        and (toward or 0) >= PROMISING_TOWARD_60
        and concentration_ok
        and (not needs_oos or window_type == WINDOW_VALIDATION)
    ):
        return LABEL_PROMISING, (
            f"n={n}, toward={toward}, fee_adjusted={fee_adj}, touch={touch}, "
            f"concentration ok"
            + ("" if not needs_oos else " on an out-of-sample window")
            + " — deserves more observation (authorizes nothing)"
        )
    if (
        frictionless is not None and frictionless > 0
        and (
            (fee_adj is not None and fee_adj <= 0)
            or (touch is not None and touch <= 0)
        )
    ):
        return LABEL_COST_KILLED, (
            f"frictionless closure {frictionless} is positive but "
            f"fee_adjusted={fee_adj} / touch={touch} — the apparent edge does "
            f"not survive assumed friction"
        )
    if (
        n >= PROMISING_MIN_N and needs_oos and window_type != WINDOW_VALIDATION
        and touch is not None and touch > 0
        and fee_adj is not None and fee_adj > 0
        and (toward or 0) >= PROMISING_TOWARD_60
        and concentration_ok
    ):
        return LABEL_NEUTRAL, (
            "clears every friction bar but this is not an out-of-sample window "
            "for a pre-registered policy — cannot be promising here"
        )
    return LABEL_NEUTRAL, (
        f"toward={toward}, frictionless={frictionless}, fee_adjusted={fee_adj}, "
        f"touch={touch} — no friction-surviving signal"
    )


class EdgeCostShadowReportService:
    """Builds the cost-adjusted shadow report. Read-only; persists nothing."""

    def _ticks_for(
        self, session: Session, row: RowDiagnostic
    ) -> tuple[MarketPriceTick | None, MarketPriceTick | None]:
        """(trigger_tick, horizon_tick): the last tick at/before the trigger
        (the book the measurement saw) and the last tick within the final
        horizon after the trigger — the same last-tick-before-deadline
        convention every follow-through module uses."""
        created = _aware(row.created_at)
        ticks = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == row.market_ticker,
                MarketPriceTick.observed_at >= created - timedelta(minutes=15),
                MarketPriceTick.observed_at <= created + timedelta(minutes=FINAL_MINUTES),
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().all()
        trigger = None
        horizon = None
        for t in ticks:
            at = _aware(t.observed_at)
            if at <= created:
                trigger = t
            elif t.midpoint is not None:
                horizon = t
        return trigger, horizon

    def build(
        self,
        session: Session,
        hours: int = 24,
        top: int = 5,
        fee_rate: float | None = None,
    ) -> dict:
        settings = get_settings()
        rate = fee_rate if fee_rate is not None else settings.kalshi_fee_rate_assumption
        now = _now()
        window_type = classify_window(
            now - timedelta(hours=hours), now, PREREG_LOCKED_AT
        )
        rows = EdgeFollowthroughDiagnosticService().build_row_diagnostics(session, hours)
        costs: list[RowCost] = []
        for r in rows:
            trigger, horizon = self._ticks_for(session, r)
            c = compute_row_cost(r, trigger, horizon, rate)
            if c is not None:
                costs.append(c)

        predicates = dict(POLICIES)
        cohorts: list[tuple[str, str, list[RowCost]]] = [
            ("baseline_all_rows", "baseline", costs)
        ]
        for name, role, _alias in PREREGISTERED:
            if role == "baseline":
                continue
            kept = [c for c in costs if predicates[name](c.row, {})]
            cohorts.append((name, "preregistered_policy", kept))
        for mt in ("total", "spread", "winner"):
            cohorts.append((
                f"market_type:{mt}", "market_type",
                [c for c in costs if c.row.market_type == mt],
            ))
        for rel in ("follows_move", "opposes_move"):
            cohorts.append((
                f"gap_{rel}", "gap_vs_move",
                [c for c in costs if c.row.gap_move_relation == rel],
            ))
        for bucket_fn, dim in (
            (_liquidity_bucket, "liquidity_bucket"),
            (_spread_bucket, "spread_bucket"),
            (_confidence_bucket, "confidence_bucket"),
        ):
            buckets: dict[str, list[RowCost]] = {}
            for c in costs:
                buckets.setdefault(bucket_fn(c.row), []).append(c)
            for key in sorted(buckets):
                cohorts.append((f"{dim}:{key}", dim, buckets[key]))
        series_counts: dict[str, list[RowCost]] = {}
        for c in costs:
            series_counts.setdefault(c.row.series, []).append(c)
        for key, members in sorted(
            series_counts.items(), key=lambda kv: -len(kv[1])
        )[:top]:
            cohorts.append((f"series:{key}", "series", members))

        summaries = []
        for name, dimension, members in cohorts:
            s = summarize_cohort(name, dimension, members)
            s["label"], s["label_reason"] = label_cohort(s, window_type)
            summaries.append(s)

        survivors = [
            s["name"] for s in summaries
            if s["final_n"] >= MIN_READABLE_FINAL_N
            and (s["fee_adjusted_net_closure_60m"] or 0) > 0
            and (s["executable_touch_closure_60m"] or 0) > 0
        ]
        return {
            "note": COST_NOTE,
            "window_hours": hours,
            "window_type": window_type,
            "fee_rate_assumption": rate,
            "population": len(rows),
            "rows_measurable": len(costs),
            "touch_coverage": _rate(
                sum(1 for c in costs if c.touch_covered), len(costs)
            ),
            "cohorts": summaries,
            "cohorts_positive_after_costs": survivors,
            "mvp_005b_note": MVP_005B_NOTE,
        }
