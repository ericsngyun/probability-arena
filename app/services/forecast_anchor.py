"""FORECAST-ANCHOR-001 — read-only forecaster-anchoring diagnostic.

FOLLOWTHROUGH-001 established that ~80% of watchlist gaps oppose the market's
own move in the prior 10 minutes even though forecasts are FRESH (p50 ~41s):
the evidence forecaster re-forecasts right after the trigger but its estimate
appears to stay anchored behind the move. This module tests that hypothesis
directly, per row, from persisted data: when the market moved between the
PRIOR forecast and this measurement, did the forecast move too — and by how
much relative to the market?

Per row it reconstructs the market midpoint at measurement and 5m/10m before,
the current and prior forecast probabilities, the deltas each made over the
same interval, and an ADJUSTMENT RATIO |forecast_delta| / |market_delta|; each
row lands in a deterministic anchor bucket (anchored_static /
partial_adjustment / moved_with_market / moved_against_market /
no_prior_forecast / insufficient_data), and cohorts get diagnosis verdicts.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): ANALYSIS AND REPORTING
ONLY. This diagnoses the forecaster's measured behavior; it changes NO
forecast, forecaster, trigger, edge-precheck gate, promotion, MarketOps/
EDGE-AUTO behavior, or flag. Every number is measured market movement or a
forecast's own recorded values — not PnL, not EV, never advice. Inputs are
existing DB rows; nothing is persisted; no external call is made.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketForecastRecord, MarketPriceTick
from app.services.edge_followthrough import (
    EdgeFollowthroughDiagnosticService,
    RowDiagnostic,
    _aware,
    _mean,
    _median,
    _rate,
)

logger = logging.getLogger(__name__)

ANCHOR_NOTE = (
    "Read-only forecaster-anchoring DIAGNOSTIC. It measures whether the "
    "forecast moved when the market moved — from recorded forecasts and ticks "
    "only. Verdicts describe the failure mechanism a cohort's data is most "
    "consistent with; they change no forecast, gate, promotion, or automation, "
    "and are never advice. Not PnL, not EV; no sizing, orders, wallets, keys, "
    "swaps, signing, or execution."
)

FINAL_HORIZON = "60m"

# Classification thresholds (deterministic; documented)
MIN_FORECAST_MOVE = 0.01     # below this the forecast is treated as static
MIN_MARKET_MOVE = 0.02       # below this there was nothing to react to
PARTIAL_RATIO = 0.5          # ratio < 0.5 => partial adjustment
BASELINE_TICK_MAX_AGE_MIN = 60  # market baseline tick must be within this of the prior forecast

BUCKET_ANCHORED = "anchored_static"
BUCKET_PARTIAL = "partial_adjustment"
BUCKET_WITH = "moved_with_market"
BUCKET_AGAINST = "moved_against_market"
BUCKET_NO_PRIOR = "no_prior_forecast"
BUCKET_INSUFFICIENT = "insufficient_data"
CLASSIFIED_BUCKETS = (BUCKET_ANCHORED, BUCKET_PARTIAL, BUCKET_WITH, BUCKET_AGAINST)

# Cohort verdicts (measurement-quality language; never advice)
VERDICT_TOO_THIN = "too_thin"
VERDICT_INSUFFICIENT_PRIOR = "insufficient_prior_forecast_data"
VERDICT_ANCHORING = "anchoring_confirmed"
VERDICT_TIMING = "timing_adverse_selection"
VERDICT_MARKET_TYPE = "market_type_specific"
VERDICT_NO_ANCHOR = "no_anchor_issue_detected"

MIN_VERDICT_CLASSIFIED = 12      # classified rows needed to read a cohort
UNCLASSIFIABLE_SHARE = 0.5       # above this the cohort lacks prior-forecast data
ANCHORING_SHARE = 0.6            # anchored+partial share to confirm anchoring
NO_ANCHOR_MOVED_WITH = 0.5       # moved_with share for "forecaster is keeping up"
TIMING_TOWARD_MAX = 0.35         # low follow-through ...
TIMING_OPPOSES_MIN = 0.6         # ... while gaps still oppose the move => timing


def classify_adjustment(
    forecast_delta: float | None, market_delta: float | None
) -> tuple[str, float | None]:
    """(anchor bucket, adjustment_ratio) for one row's deltas over the SAME
    interval (prior forecast -> measurement). Deterministic:
    - market barely moved (< MIN_MARKET_MOVE)     -> insufficient_data (no test)
    - |forecast_delta| < MIN_FORECAST_MOVE        -> anchored_static
    - opposite signs                              -> moved_against_market
    - same sign, ratio < PARTIAL_RATIO            -> partial_adjustment
    - same sign, ratio >= PARTIAL_RATIO           -> moved_with_market"""
    if forecast_delta is None or market_delta is None:
        return BUCKET_INSUFFICIENT, None
    if abs(market_delta) < MIN_MARKET_MOVE:
        return BUCKET_INSUFFICIENT, None
    ratio = round(abs(forecast_delta) / abs(market_delta), 4)
    if abs(forecast_delta) < MIN_FORECAST_MOVE:
        return BUCKET_ANCHORED, ratio
    if (forecast_delta > 0) != (market_delta > 0):
        return BUCKET_AGAINST, ratio
    if ratio < PARTIAL_RATIO:
        return BUCKET_PARTIAL, ratio
    return BUCKET_WITH, ratio


@dataclass
class AnchorRow:
    """One watchlist row's anchoring reconstruction. Measurements only."""

    base: RowDiagnostic
    mid_at_measure: float | None = None
    mid_5m_before: float | None = None
    mid_10m_before: float | None = None
    forecast_probability: float | None = None
    prior_forecast_probability: float | None = None
    prior_forecast_age_s: float | None = None   # prior forecast -> measurement
    forecast_delta: float | None = None
    market_delta: float | None = None           # over the prior-forecast interval
    adjustment_ratio: float | None = None
    bucket: str = BUCKET_INSUFFICIENT
    market_moved_more: bool | None = None


class ForecastAnchorDiagnosticService:
    """Builds the anchoring diagnostic from FOLLOWTHROUGH-001 row diagnostics
    plus prior-forecast and tick reconstruction. Read-only; changes nothing."""

    def _mid_before(self, session: Session, row: RowDiagnostic, minutes: int) -> float | None:
        """Earliest midpoint tick within `minutes` before measurement."""
        created = _aware(row.created_at)
        earlier = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == row.market_ticker,
                MarketPriceTick.observed_at >= created - timedelta(minutes=minutes),
                MarketPriceTick.observed_at <= created,
                MarketPriceTick.midpoint.is_not(None),
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().first()
        return earlier.midpoint if earlier is not None else None

    def _market_baseline(
        self, session: Session, ticker: str, at: datetime
    ) -> float | None:
        """Last midpoint tick at/before `at` (within a bounded lookback), i.e.
        where the market stood when the PRIOR forecast was made."""
        tick = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == ticker,
                MarketPriceTick.observed_at <= at,
                MarketPriceTick.observed_at >= at - timedelta(minutes=BASELINE_TICK_MAX_AGE_MIN),
                MarketPriceTick.midpoint.is_not(None),
            )
            .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
        ).scalars().first()
        return tick.midpoint if tick is not None else None

    def build_rows(self, session: Session, hours: int) -> list[AnchorRow]:
        base_rows = EdgeFollowthroughDiagnosticService().build_row_diagnostics(session, hours)

        forecast_ids = {r.forecast_id for r in base_rows if r.forecast_id is not None}
        forecasts: dict[int, MarketForecastRecord] = {}
        if forecast_ids:
            for f in session.execute(
                select(MarketForecastRecord).where(MarketForecastRecord.id.in_(forecast_ids))
            ).scalars().all():
                forecasts[f.id] = f

        out: list[AnchorRow] = []
        for r in base_rows:
            a = AnchorRow(
                base=r,
                mid_at_measure=None,
                forecast_probability=r.forecast_probability,
            )
            # midpoint at measure is on the snapshot itself (via gap = f - mid)
            if r.forecast_probability is not None and r.gap is not None:
                a.mid_at_measure = round(r.forecast_probability - r.gap, 4)
            a.mid_5m_before = self._mid_before(session, r, 5)
            a.mid_10m_before = self._mid_before(session, r, 10)

            current = forecasts.get(r.forecast_id)
            prior = None
            if current is not None:
                prior = session.execute(
                    select(MarketForecastRecord)
                    .where(
                        MarketForecastRecord.market_ticker == r.market_ticker,
                        MarketForecastRecord.created_at < current.created_at,
                    )
                    .order_by(MarketForecastRecord.created_at.desc(), MarketForecastRecord.id.desc())
                ).scalars().first()

            if prior is None:
                a.bucket = BUCKET_NO_PRIOR
                out.append(a)
                continue

            a.prior_forecast_probability = prior.estimated_probability
            prior_at = _aware(prior.created_at)
            a.prior_forecast_age_s = round(
                (_aware(r.created_at) - prior_at).total_seconds(), 1
            )
            baseline_mid = self._market_baseline(session, r.market_ticker, prior_at)
            if (
                baseline_mid is None
                or a.mid_at_measure is None
                or a.forecast_probability is None
                or prior.estimated_probability is None
            ):
                a.bucket = BUCKET_INSUFFICIENT
                out.append(a)
                continue

            a.forecast_delta = round(a.forecast_probability - prior.estimated_probability, 4)
            a.market_delta = round(a.mid_at_measure - baseline_mid, 4)
            a.bucket, a.adjustment_ratio = classify_adjustment(a.forecast_delta, a.market_delta)
            if a.bucket in CLASSIFIED_BUCKETS:
                a.market_moved_more = abs(a.market_delta) > abs(a.forecast_delta)
            out.append(a)
        return out

    # --- aggregation ---------------------------------------------------------

    @staticmethod
    def _cohort_summary(rows: list[AnchorRow]) -> dict:
        classified = [a for a in rows if a.bucket in CLASSIFIED_BUCKETS]
        n_class = len(classified)
        buckets = {b: sum(1 for a in rows if a.bucket == b) for b in (
            BUCKET_ANCHORED, BUCKET_PARTIAL, BUCKET_WITH, BUCKET_AGAINST,
            BUCKET_NO_PRIOR, BUCKET_INSUFFICIENT,
        )}
        final = [a for a in rows if FINAL_HORIZON in a.base.closures]
        toward = sum(1 for a in final if a.base.closures[FINAL_HORIZON] > 0)
        opposes = sum(1 for a in rows if a.base.gap_move_relation == "opposes_move")
        follows = sum(1 for a in rows if a.base.gap_move_relation == "follows_move")

        def bucket_follow(bucket: str) -> dict:
            members = [
                a for a in rows if a.bucket == bucket and FINAL_HORIZON in a.base.closures
            ]
            return {
                "n": len(members),
                "toward_rate_60m": _rate(
                    sum(1 for a in members if a.base.closures[FINAL_HORIZON] > 0),
                    len(members),
                ),
                "mean_closure_60m": _mean(
                    [a.base.closures[FINAL_HORIZON] for a in members]
                ),
            }

        return {
            "n": len(rows),
            "classified_n": n_class,
            "unclassifiable_share": _rate(
                buckets[BUCKET_NO_PRIOR] + buckets[BUCKET_INSUFFICIENT], len(rows)
            ),
            "bucket_counts": buckets,
            "bucket_shares": {
                b: _rate(buckets[b], n_class) for b in CLASSIFIED_BUCKETS
            },
            "median_forecast_delta": _median(
                [a.forecast_delta for a in classified]
            ),
            "median_market_delta": _median([a.market_delta for a in classified]),
            "median_adjustment_ratio": _median(
                [a.adjustment_ratio for a in classified]
            ),
            "market_moved_more_share": _rate(
                sum(1 for a in classified if a.market_moved_more), n_class
            ),
            "toward_rate_60m": _rate(toward, len(final)),
            "gap_opposes_move_share": _rate(opposes, opposes + follows),
            "follow_through_by_bucket": {
                b: bucket_follow(b) for b in CLASSIFIED_BUCKETS
            },
        }

    @staticmethod
    def verdict_for(summary: dict) -> tuple[str, str]:
        """Deterministic cohort diagnosis. Priority (documented):
        too_thin -> insufficient_prior_forecast_data -> anchoring_confirmed ->
        no_anchor_issue_detected (forecaster keeps up) -> timing_adverse_selection
        (forecaster keeps up or is mixed, but selection is still bad) ->
        no_anchor_issue_detected fallback. Measurement language only."""
        if summary["classified_n"] < MIN_VERDICT_CLASSIFIED:
            if (summary["unclassifiable_share"] or 0) > UNCLASSIFIABLE_SHARE and summary["n"] >= MIN_VERDICT_CLASSIFIED:
                return VERDICT_INSUFFICIENT_PRIOR, (
                    f"{summary['unclassifiable_share']:.0%} of rows lack a usable prior "
                    f"forecast/baseline — anchoring cannot be measured here"
                )
            return VERDICT_TOO_THIN, (
                f"only {summary['classified_n']} classifiable rows (<{MIN_VERDICT_CLASSIFIED})"
            )
        shares = summary["bucket_shares"]
        anchored_share = (shares.get(BUCKET_ANCHORED) or 0) + (shares.get(BUCKET_PARTIAL) or 0)
        with_share = shares.get(BUCKET_WITH) or 0
        toward = summary["toward_rate_60m"]
        opposes = summary["gap_opposes_move_share"]

        if anchored_share >= ANCHORING_SHARE:
            return VERDICT_ANCHORING, (
                f"{anchored_share:.0%} of classifiable rows are anchored_static or "
                f"partial_adjustment (median adjustment ratio "
                f"{summary['median_adjustment_ratio']}) — the forecast measurably "
                f"fails to keep up with the market's move"
            )
        if with_share >= NO_ANCHOR_MOVED_WITH:
            if (
                toward is not None and toward <= TIMING_TOWARD_MAX
                and opposes is not None and opposes >= TIMING_OPPOSES_MIN
            ):
                return VERDICT_TIMING, (
                    f"the forecast keeps up ({with_share:.0%} moved_with_market) yet "
                    f"follow-through is still poor (toward {toward}) with gaps opposing "
                    f"the move ({opposes:.0%}) — the residual failure is trigger "
                    f"timing/selection, not anchoring"
                )
            return VERDICT_NO_ANCHOR, (
                f"{with_share:.0%} of classifiable rows moved with the market and no "
                f"low-follow-through/opposes pattern dominates"
            )
        if (
            toward is not None and toward <= TIMING_TOWARD_MAX
            and opposes is not None and opposes >= TIMING_OPPOSES_MIN
        ):
            return VERDICT_TIMING, (
                f"mixed adjustment (anchored+partial {anchored_share:.0%}, moved_with "
                f"{with_share:.0%}) but follow-through stays poor (toward {toward}) with "
                f"gaps opposing the move ({opposes:.0%}) — selection/timing dominates"
            )
        return VERDICT_NO_ANCHOR, (
            f"no dominant anchoring pattern (anchored+partial {anchored_share:.0%}, "
            f"moved_with {with_share:.0%})"
        )

    # --- report --------------------------------------------------------------

    def build(self, session: Session, hours: int = 24, top: int = 5) -> dict:
        rows = self.build_rows(session, hours)

        def group(key_fn) -> dict[str, list[AnchorRow]]:
            groups: dict[str, list[AnchorRow]] = {}
            for a in rows:
                groups.setdefault(key_fn(a), []).append(a)
            return groups

        from app.services.edge_cohort import (
            _confidence_bucket,
            _liquidity_bucket,
            _spread_bucket,
        )

        dim_key_fns = {
            "market_type": lambda a: a.base.market_type,
            "series": lambda a: a.base.series,
            "signal_type": lambda a: a.base.signal_type,
            "gap_vs_pre_move": lambda a: a.base.gap_move_relation,
            "pre_move": lambda a: "sharp_pre_move" if a.base.sharp_pre_move else "calm_pre_move",
            "liquidity_bucket": lambda a: _liquidity_bucket(a.base.liquidity_cents),
            "spread_bucket": lambda a: _spread_bucket(a.base.spread_cents),
            "confidence_bucket": lambda a: _confidence_bucket(a.base.confidence),
            "game_phase": lambda a: a.base.game_phase,
            "gap_sign": lambda a: "positive" if (a.base.gap or 0) >= 0 else "negative",
            "forecaster": lambda a: a.base.forecaster,
        }
        dimensions: dict[str, dict] = {}
        for dim, key_fn in dim_key_fns.items():
            cohorts = {}
            for key, members in sorted(group(key_fn).items(), key=lambda kv: -len(kv[1])):
                summary = self._cohort_summary(members)
                summary["verdict"], summary["verdict_reason"] = self.verdict_for(summary)
                cohorts[key] = summary
            dimensions[dim] = cohorts

        overall = self._cohort_summary(rows)
        overall["verdict"], overall["verdict_reason"] = self.verdict_for(overall)
        # market_type_specific override at the OVERALL level: adequately-sampled
        # market-type cohorts that DISAGREE mean one type carries the problem.
        mt = dimensions.get("market_type", {})
        mt_verdicts = {
            k: v["verdict"] for k, v in mt.items()
            if v["classified_n"] >= MIN_VERDICT_CLASSIFIED
        }
        if len(set(mt_verdicts.values())) > 1 and VERDICT_ANCHORING in mt_verdicts.values():
            overall["verdict"] = VERDICT_MARKET_TYPE
            overall["verdict_reason"] = (
                f"market types disagree: {mt_verdicts} — the anchoring problem is "
                f"concentrated in specific market types, not uniform"
            )

        return {
            "note": ANCHOR_NOTE,
            "window_hours": hours,
            "rows": len(rows),
            "overall": overall,
            "dimensions": dimensions,
            "examples": self._examples(rows, top),
            "interpretation": self._interpret(overall, dimensions),
        }

    # --- examples --------------------------------------------------------------

    @staticmethod
    def _view(a: AnchorRow) -> dict:
        return {
            "ticker": a.base.market_ticker,
            "bucket": a.bucket,
            "forecast_delta": a.forecast_delta,
            "market_delta": a.market_delta,
            "adjustment_ratio": a.adjustment_ratio,
            "gap": a.base.gap,
            "closure_60m": a.base.closures.get(FINAL_HORIZON),
            "market_type": a.base.market_type,
        }

    def _examples(self, rows: list[AnchorRow], top: int) -> dict:
        classified = [a for a in rows if a.bucket in CLASSIFIED_BUCKETS]
        with_final = [a for a in classified if FINAL_HORIZON in a.base.closures]
        anchored = [a for a in with_final if a.bucket in (BUCKET_ANCHORED, BUCKET_PARTIAL)]
        moved = [a for a in with_final if a.bucket == BUCKET_WITH]

        worst_anchored = sorted(anchored, key=lambda a: a.base.closures[FINAL_HORIZON])[:top]
        adjusted_ok = sorted(moved, key=lambda a: -(a.adjustment_ratio or 0))[:top]
        sharp_static = sorted(
            (a for a in classified
             if a.market_delta is not None and abs(a.market_delta) >= 0.05
             and a.bucket == BUCKET_ANCHORED),
            key=lambda a: -abs(a.market_delta or 0),
        )[:top]
        adjusted_but_failed = sorted(
            (a for a in moved if a.base.closures.get(FINAL_HORIZON, 0) < 0),
            key=lambda a: a.base.closures[FINAL_HORIZON],
        )[:top]
        totals = [a for a in with_final if a.base.market_type == "total"][:top]
        spreads = [a for a in with_final if a.base.market_type == "spread"][:top]

        return {
            "worst_anchored_behind_market": [self._view(a) for a in worst_anchored],
            "forecasts_that_adjusted": [self._view(a) for a in adjusted_ok],
            "sharp_market_move_forecast_static": [self._view(a) for a in sharp_static],
            "adjusted_but_followthrough_failed": [self._view(a) for a in adjusted_but_failed],
            "totals_examples": [self._view(a) for a in totals],
            "spread_examples": [self._view(a) for a in spreads],
        }

    # --- interpretation ----------------------------------------------------------

    @staticmethod
    def _interpret(overall: dict, dimensions: dict) -> dict:
        """Programmatic answers from computed numbers. Measurement only."""
        shares = overall["bucket_shares"]
        anchored_share = (shares.get(BUCKET_ANCHORED) or 0) + (shares.get(BUCKET_PARTIAL) or 0)
        by_bucket = overall["follow_through_by_bucket"]
        mt = dimensions.get("market_type", {})
        spread = mt.get("spread", {})
        total = mt.get("total", {})

        def mt_anchored(c: dict) -> float | None:
            s = c.get("bucket_shares") or {}
            if s.get(BUCKET_ANCHORED) is None and s.get(BUCKET_PARTIAL) is None:
                return None
            return round((s.get(BUCKET_ANCHORED) or 0) + (s.get(BUCKET_PARTIAL) or 0), 4)

        anchored_ft = by_bucket.get(BUCKET_ANCHORED, {})
        partial_ft = by_bucket.get(BUCKET_PARTIAL, {})
        with_ft = by_bucket.get(BUCKET_WITH, {})

        # is the next-step evidence pointing at forecaster, trigger, or more data?
        verdict = overall["verdict"]
        if verdict == VERDICT_ANCHORING:
            next_evidence = (
                "forecaster_redesign_candidate: anchoring dominates — but any "
                "change is a separate explicitly-accepted milestone"
            )
        elif verdict in (VERDICT_TIMING, VERDICT_MARKET_TYPE):
            next_evidence = (
                "trigger_redesign_candidate_or_more_data: adjustment is not the "
                "dominant failure; selection/timing (or one market type) is — "
                "keep collecting shadow data before any change"
            )
        elif verdict in (VERDICT_TOO_THIN, VERDICT_INSUFFICIENT_PRIOR):
            next_evidence = "more_data: the anchoring question is not yet measurable"
        else:
            next_evidence = "more_shadow_data: no dominant mechanism to act on"

        return {
            "forecaster_failing_to_move_with_market": {
                "anchored_plus_partial_share": round(anchored_share, 4),
                "median_adjustment_ratio": overall["median_adjustment_ratio"],
                "market_moved_more_share": overall["market_moved_more_share"],
            },
            "anchoring_explains_negative_followthrough": {
                "anchored_toward_60m": anchored_ft.get("toward_rate_60m"),
                "partial_toward_60m": partial_ft.get("toward_rate_60m"),
                "moved_with_toward_60m": with_ft.get("toward_rate_60m"),
                "note": "if anchored/partial rows underperform moved_with rows, anchoring contributes",
            },
            "spreads_more_anchored_than_totals": {
                "spread_anchored_share": mt_anchored(spread),
                "total_anchored_share": mt_anchored(total),
                "spread_verdict": spread.get("verdict"),
                "total_verdict": total.get("verdict"),
            },
            "totals_less_anchored_or_just_less_adverse": {
                "total_anchored_share": mt_anchored(total),
                "total_gap_opposes_share": total.get("gap_opposes_move_share"),
                "total_toward_60m": total.get("toward_rate_60m"),
            },
            "positive_shadow_cohorts_explained_by_adjustment": {
                "moved_with_closure_60m": with_ft.get("mean_closure_60m"),
                "anchored_closure_60m": anchored_ft.get("mean_closure_60m"),
            },
            "next_step_evidence": next_evidence,
        }
