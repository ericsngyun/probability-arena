"""FOLLOWTHROUGH-001 — read-only diagnostic for negative gap follow-through.

The frontier/cohort reports established THAT follow-through is negative (the
market midpoint tends to move AWAY from our forecast after a watchlist gap is
measured). This module diagnoses WHY, per cohort, from already-persisted rows:

* TIMING — how old were the signal / forecast / market snapshot at measurement,
  and did the market move sharply in the minutes BEFORE we measured? A gap that
  points back to where the market just came from ("gap opposes the recent
  move") is the signature of a stale forecast chasing a move rather than
  anticipating one.
* DIRECTION — after measurement, did the midpoint continue away from the
  forecast, revert toward it, or stay flat? Did the spread widen and the
  liquidity change?
* VERDICTS — deterministic per-cohort diagnosis labels
  (adverse_selection_candidate / stale_or_chasing_move / too_thin / neutral /
  promising_needs_more_sample / measurement_artifact_possible) describing which
  failure mechanism the cohort's data is most consistent with.
* FAILURE EXAMPLES — the concrete rows behind the aggregate numbers.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): ANALYSIS AND REPORTING
ONLY. Follow-through and every diagnostic here is market-MOVEMENT measurement —
NOT PnL, no fills, no positions, no sizing, no dollar figures. A verdict is a
statement about MEASUREMENT QUALITY and failure mechanism, never advice, never
an instruction, and it changes no gate, threshold, promotion, forecast, or
MarketOps/EDGE-AUTO behavior. Inputs are existing DB rows only; no external
call is made.

Methodology note: post-measurement movement uses the same convention as the
frontier/cohort analyses (last tick at/before each horizon deadline;
closure = (later_mid − measured_mid) / signed_gap; toward ⇔ closure > 0), so
numbers here reconcile with those reports.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.edge_cohort import (
    FOLLOW_THROUGH_HORIZONS_MINUTES,
    FOLLOW_THROUGH_STATUSES,
    _abs_gap_bucket,
    _confidence_bucket,
    _game_phase,
    _liquidity_bucket,
    _spread_bucket,
    _tag_value,
)

logger = logging.getLogger(__name__)

DIAGNOSTIC_NOTE = (
    "Read-only follow-through DIAGNOSTIC. Every number is measured market "
    "movement around our own measurement moments — not PnL, not EV, not a "
    "signal, not advice. Verdicts describe the failure mechanism a cohort's "
    "data is most consistent with; they authorize nothing and change no gate, "
    "forecast, promotion, or automation behavior. No sizing, orders, wallets, "
    "keys, swaps, signing, or execution."
)

# Pre-measurement window: was the market already moving when we measured?
PRE_MOVE_WINDOW_MINUTES = 10
SHARP_PRE_MOVE = 0.03          # |mid delta| in the pre-window that counts as a sharp move
# Post-measurement path classification per horizon (on closure fraction):
CONTINUED_AWAY_CLOSURE = -0.25  # closure at/below => midpoint continued away
REVERTED_TOWARD_CLOSURE = 0.25  # closure at/above => midpoint reverted toward forecast

# Verdict thresholds (deterministic; documented; measurement-quality language only)
MIN_VERDICT_SAMPLES = 12        # matches the cohort report's too_thin floor
ADVERSE_TOWARD_RATE = 0.35      # at/below, with continuation dominating => adverse selection
CONTINUATION_DOMINANT = 0.50    # share of 60m rows that continued away
CHASING_SHARE = 0.60            # share of rows whose gap opposes the recent move
STALE_FORECAST_P50_SECONDS = 240
ARTIFACT_SNAPSHOT_P50_SECONDS = 60
PROMISING_TOWARD_RATE = 0.50
PROMISING_MAX_SAMPLES = 30

VERDICT_TOO_THIN = "too_thin"
VERDICT_ADVERSE_SELECTION = "adverse_selection_candidate"
VERDICT_STALE_OR_CHASING = "stale_or_chasing_move"
VERDICT_ARTIFACT = "measurement_artifact_possible"
VERDICT_PROMISING = "promising_needs_more_sample"
VERDICT_NEUTRAL = "neutral"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _median(values: list) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return round(float(vals[mid]), 4)
    return round((vals[mid - 1] + vals[mid]) / 2, 4)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _age_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 120:
        return "<2m"
    if seconds < 300:
        return "2-5m"
    if seconds < 900:
        return "5-15m"
    return ">15m"


def _snapshot_age_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 15:
        return "<15s"
    if seconds <= 60:
        return "15-60s"
    return ">60s"


def classify_path(closure: float | None) -> str:
    """Post-measurement path per horizon, from the closure fraction.
    continued_away: midpoint moved further from the forecast by >=25% of the gap;
    reverted_toward: midpoint closed >=25% of the gap; else flat."""
    if closure is None:
        return "no_sample"
    if closure <= CONTINUED_AWAY_CLOSURE:
        return "continued_away"
    if closure >= REVERTED_TOWARD_CLOSURE:
        return "reverted_toward"
    return "flat"


def gap_vs_pre_move(gap: float | None, pre_move: float | None) -> str:
    """Relationship between the measured gap and the market's move in the
    minutes BEFORE measurement.

    opposes_move: the gap points back to where the market came from — i.e. the
    market moved and the (older) forecast did not follow, so the "gap" is the
    forecast lagging the move. follows_move: gap points the same way the market
    was already going. no_pre_move / unknown otherwise."""
    if gap is None or pre_move is None:
        return "unknown"
    if abs(pre_move) < 1e-9:
        return "no_pre_move"
    if abs(gap) < 1e-9:
        return "no_gap"
    return "opposes_move" if (gap > 0) != (pre_move > 0) else "follows_move"


@dataclass
class RowDiagnostic:
    """Everything measured about one watchlist/candidate snapshot. Counters and
    measurements only — no advice fields exist."""

    snapshot_id: int
    market_ticker: str
    series: str
    created_at: datetime
    market_type: str = "unknown"
    domain: str = "unknown"
    signal_type: str = "unknown"
    game_phase: str = "unknown"
    forecaster: str = "unknown"
    evidence_depth: str = "unknown"
    persistence: int = 1
    confidence: float | None = None
    gap: float | None = None
    abs_gap: float | None = None
    forecast_id: int | None = None           # for prior-forecast joins (FORECAST-ANCHOR-001)
    forecast_probability: float | None = None
    spread_cents: int | None = None
    liquidity_cents: int | None = None
    # timing
    forecast_age_s: int | None = None
    snapshot_age_s: int | None = None
    signal_age_s: float | None = None
    # pre-measurement market path
    pre_move: float | None = None          # mid(measure) - mid(measure - window)
    sharp_pre_move: bool = False
    gap_move_relation: str = "unknown"     # opposes_move / follows_move / ...
    # post-measurement path (per horizon label -> closure)
    closures: dict = field(default_factory=dict)
    paths: dict = field(default_factory=dict)
    worst_closure: float | None = None
    spread_change_60m: int | None = None
    liquidity_change_60m: int | None = None


class EdgeFollowthroughDiagnosticService:
    """Builds the per-row diagnostics and the cohort verdict report.
    Read-only over persisted rows; changes nothing."""

    def _load_rows(self, session: Session, hours: int) -> list[EdgePrecheckSnapshot]:
        start = _now() - timedelta(hours=hours)
        return session.execute(
            select(EdgePrecheckSnapshot)
            .where(
                EdgePrecheckSnapshot.created_at >= start,
                EdgePrecheckSnapshot.status.in_(FOLLOW_THROUGH_STATUSES),
                EdgePrecheckSnapshot.probability_gap.is_not(None),
                EdgePrecheckSnapshot.market_midpoint.is_not(None),
            )
            .order_by(EdgePrecheckSnapshot.id.asc())
        ).scalars().all()

    # --- per-row measurement --------------------------------------------------

    def _pre_move(self, session: Session, row: EdgePrecheckSnapshot) -> float | None:
        """Midpoint change over the PRE_MOVE_WINDOW before measurement: the
        earliest midpoint tick inside the window vs the measured midpoint."""
        created = _aware(row.created_at)
        window_start = created - timedelta(minutes=PRE_MOVE_WINDOW_MINUTES)
        earlier = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == row.market_ticker,
                MarketPriceTick.observed_at >= window_start,
                MarketPriceTick.observed_at <= created,
                MarketPriceTick.midpoint.is_not(None),
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().first()
        if earlier is None or row.market_midpoint is None:
            return None
        return round(row.market_midpoint - earlier.midpoint, 4)

    def _post_path(
        self, session: Session, row: EdgePrecheckSnapshot
    ) -> tuple[dict, dict, int | None, int | None]:
        """Per-horizon closures + path labels, and the 60m spread/liquidity
        change. Same last-tick-before-deadline convention as frontier/cohort."""
        closures: dict[str, float] = {}
        paths: dict[str, str] = {}
        spread_change = liquidity_change = None
        created = _aware(row.created_at)
        for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
            deadline = created + timedelta(minutes=minutes)
            later = session.execute(
                select(MarketPriceTick)
                .where(
                    MarketPriceTick.market_ticker == row.market_ticker,
                    MarketPriceTick.observed_at > created,
                    MarketPriceTick.observed_at <= deadline,
                    MarketPriceTick.midpoint.is_not(None),
                )
                .order_by(MarketPriceTick.observed_at.desc(), MarketPriceTick.id.desc())
            ).scalars().first()
            label = f"{minutes}m"
            if later is None:
                paths[label] = "no_sample"
                continue
            delta = later.midpoint - row.market_midpoint
            closure = round(delta / row.probability_gap, 4) if row.probability_gap else 0.0
            closures[label] = closure
            paths[label] = classify_path(closure)
            if minutes == FOLLOW_THROUGH_HORIZONS_MINUTES[-1]:
                if later.spread is not None and row.spread_cents is not None:
                    spread_change = later.spread - row.spread_cents
                if later.liquidity_proxy is not None and row.liquidity_proxy_cents is not None:
                    liquidity_change = later.liquidity_proxy - row.liquidity_proxy_cents
        return closures, paths, spread_change, liquidity_change

    def build_row_diagnostics(self, session: Session, hours: int) -> list[RowDiagnostic]:
        rows = self._load_rows(session, hours)

        signal_ids = {r.signal_id for r in rows if r.signal_id is not None}
        signals: dict[int, OpportunitySignal] = {}
        if signal_ids:
            for sig in session.execute(
                select(OpportunitySignal).where(OpportunitySignal.id.in_(signal_ids))
            ).scalars().all():
                signals[sig.id] = sig

        forecast_ids = {r.forecast_id for r in rows if r.forecast_id is not None}
        phase_by_forecast: dict[int, str] = {}
        if forecast_ids:
            for fid, tags in session.execute(
                select(
                    MarketForecastRecord.id, MarketForecastRecord.calibration_tags
                ).where(MarketForecastRecord.id.in_(forecast_ids))
            ).all():
                phase_by_forecast[fid] = _game_phase(tags)

        out: list[RowDiagnostic] = []
        for row in rows:
            created = _aware(row.created_at)
            sig = signals.get(row.signal_id)
            signal_age = None
            if sig is not None and sig.created_at is not None:
                signal_age = round((created - _aware(sig.created_at)).total_seconds(), 1)
            pre_move = self._pre_move(session, row)
            closures, paths, spread_chg, liq_chg = self._post_path(session, row)
            diag = RowDiagnostic(
                snapshot_id=row.id,
                market_ticker=row.market_ticker,
                series=(row.market_ticker or "").split("-", 1)[0] or "unknown",
                created_at=created,
                market_type=_tag_value(row.tags, "market_type:"),
                domain=_tag_value(row.tags, "domain:"),
                signal_type=(sig.signal_type if sig is not None else "unknown") or "unknown",
                game_phase=phase_by_forecast.get(row.forecast_id, "unknown"),
                forecaster=row.forecaster_name or "unknown",
                evidence_depth=row.evidence_depth or "unknown",
                persistence=row.persistence_count or 1,
                confidence=row.forecast_confidence,
                gap=row.probability_gap,
                abs_gap=row.abs_probability_gap,
                forecast_id=row.forecast_id,
                forecast_probability=row.forecast_probability,
                spread_cents=row.spread_cents,
                liquidity_cents=row.liquidity_proxy_cents,
                forecast_age_s=row.forecast_age_seconds,
                snapshot_age_s=row.market_snapshot_age_seconds,
                signal_age_s=signal_age,
                pre_move=pre_move,
                sharp_pre_move=(pre_move is not None and abs(pre_move) >= SHARP_PRE_MOVE),
                gap_move_relation=gap_vs_pre_move(row.probability_gap, pre_move),
                closures=closures,
                paths=paths,
                worst_closure=(min(closures.values()) if closures else None),
                spread_change_60m=spread_chg,
                liquidity_change_60m=liq_chg,
            )
            out.append(diag)
        return out

    # --- cohort aggregation + verdicts -----------------------------------------

    @staticmethod
    def _cohort_summary(rows: list[RowDiagnostic]) -> dict:
        final_label = f"{FOLLOW_THROUGH_HORIZONS_MINUTES[-1]}m"
        sampled = [r for r in rows if r.closures]
        final = [r for r in rows if final_label in r.closures]
        toward_final = sum(1 for r in final if r.closures[final_label] > 0)
        continued = sum(1 for r in final if r.paths.get(final_label) == "continued_away")
        reverted = sum(1 for r in final if r.paths.get(final_label) == "reverted_toward")
        opposes = sum(1 for r in rows if r.gap_move_relation == "opposes_move")
        follows = sum(1 for r in rows if r.gap_move_relation == "follows_move")
        sharp = sum(1 for r in rows if r.sharp_pre_move)
        return {
            "n": len(rows),
            "follow_n": len(sampled),
            "final_n": len(final),
            "toward_rate_final": _rate(toward_final, len(final)),
            "mean_closure_final": _mean([r.closures.get(final_label) for r in final]),
            "continued_away_rate": _rate(continued, len(final)),
            "reverted_toward_rate": _rate(reverted, len(final)),
            "gap_opposes_move_share": _rate(opposes, opposes + follows),
            "sharp_pre_move_share": _rate(sharp, len(rows)),
            "forecast_age_p50_s": _median([r.forecast_age_s for r in rows]),
            "signal_age_p50_s": _median([r.signal_age_s for r in rows]),
            "snapshot_age_p50_s": _median([r.snapshot_age_s for r in rows]),
            "mean_abs_gap": _mean([r.abs_gap for r in rows]),
            "spread_change_60m_mean": _mean([r.spread_change_60m for r in rows]),
            "liquidity_change_60m_mean": _mean([r.liquidity_change_60m for r in rows]),
        }

    @staticmethod
    def verdict_for(summary: dict) -> tuple[str, str]:
        """Deterministic diagnosis for one cohort. Priority order matters and is
        documented: thin data first, then the failure mechanisms from most to
        least specific, then promise, then neutral. Measurement-quality language
        only — a verdict never authorizes or recommends anything."""
        n = summary["final_n"]
        if n < MIN_VERDICT_SAMPLES:
            return VERDICT_TOO_THIN, f"only {n} final-horizon samples (<{MIN_VERDICT_SAMPLES})"
        toward = summary["toward_rate_final"] or 0.0
        continued = summary["continued_away_rate"] or 0.0
        opposes = summary["gap_opposes_move_share"]
        forecast_p50 = summary["forecast_age_p50_s"]
        snapshot_p50 = summary["snapshot_age_p50_s"]

        if (
            opposes is not None and opposes >= CHASING_SHARE
            and forecast_p50 is not None and forecast_p50 > STALE_FORECAST_P50_SECONDS
        ):
            return VERDICT_STALE_OR_CHASING, (
                f"gap opposes the pre-measurement move in {opposes:.0%} of rows and "
                f"forecast age p50={forecast_p50:.0f}s — the 'gap' is mostly an old "
                f"forecast lagging a move the market already made"
            )
        if toward <= ADVERSE_TOWARD_RATE and continued >= CONTINUATION_DOMINANT:
            return VERDICT_ADVERSE_SELECTION, (
                f"toward_rate {toward:.2f} with {continued:.0%} of rows continuing away — "
                f"measurement moments are selected by market movement that then persists "
                f"(the trigger fires exactly when the market is trending against the forecast)"
            )
        if snapshot_p50 is not None and snapshot_p50 > ARTIFACT_SNAPSHOT_P50_SECONDS:
            return VERDICT_ARTIFACT, (
                f"market snapshot age p50={snapshot_p50:.0f}s at measurement — gaps may be "
                f"computed against stale quotes; treat cohort numbers with suspicion"
            )
        if toward >= PROMISING_TOWARD_RATE and n < PROMISING_MAX_SAMPLES:
            return VERDICT_PROMISING, (
                f"toward_rate {toward:.2f} over only n={n} — keep observing before reading anything into it"
            )
        return VERDICT_NEUTRAL, f"toward_rate {toward:.2f}, no dominant failure mechanism"

    def build(self, session: Session, hours: int = 24, top: int = 5) -> dict:
        rows = self.build_row_diagnostics(session, hours)

        def group(key_fn) -> dict[str, list[RowDiagnostic]]:
            groups: dict[str, list[RowDiagnostic]] = {}
            for r in rows:
                groups.setdefault(key_fn(r), []).append(r)
            return groups

        dimensions: dict[str, dict] = {}
        dim_key_fns = {
            "market_type": lambda r: r.market_type,
            "gap_sign": lambda r: "positive" if (r.gap or 0) >= 0 else "negative",
            "abs_gap_bucket": lambda r: _abs_gap_bucket(r.abs_gap),
            "confidence_bucket": lambda r: _confidence_bucket(r.confidence),
            "spread_bucket": lambda r: _spread_bucket(r.spread_cents),
            "liquidity_bucket": lambda r: _liquidity_bucket(r.liquidity_cents),
            "signal_type": lambda r: r.signal_type,
            "signal_age_bucket": lambda r: _age_bucket(r.signal_age_s),
            "forecast_age_bucket": lambda r: _age_bucket(r.forecast_age_s),
            "snapshot_age_bucket": lambda r: _snapshot_age_bucket(r.snapshot_age_s),
            "game_phase": lambda r: r.game_phase,
            "series": lambda r: r.series,
            "persistence": lambda r: str(r.persistence) if r.persistence < 3 else "3+",
            "forecaster": lambda r: r.forecaster,
            "evidence": lambda r: r.evidence_depth,
            "gap_vs_pre_move": lambda r: r.gap_move_relation,
        }
        for dim, key_fn in dim_key_fns.items():
            cohorts = {}
            for key, members in sorted(group(key_fn).items(), key=lambda kv: -len(kv[1])):
                summary = self._cohort_summary(members)
                verdict, why = self.verdict_for(summary)
                summary["verdict"] = verdict
                summary["verdict_reason"] = why
                cohorts[key] = summary
            dimensions[dim] = cohorts

        overall = self._cohort_summary(rows)
        overall_verdict, overall_why = self.verdict_for(overall)
        overall["verdict"] = overall_verdict
        overall["verdict_reason"] = overall_why

        return {
            "note": DIAGNOSTIC_NOTE,
            "window_hours": hours,
            "rows": len(rows),
            "overall": overall,
            "dimensions": dimensions,
            "failure_examples": self._failure_examples(rows, top),
        }

    # --- failure examples --------------------------------------------------------

    @staticmethod
    def _row_view(r: RowDiagnostic) -> dict:
        return {
            "snapshot_id": r.snapshot_id,
            "ticker": r.market_ticker,
            "created_at": r.created_at.isoformat(),
            "gap": r.gap,
            "pre_move": r.pre_move,
            "gap_vs_pre_move": r.gap_move_relation,
            "forecast_age_s": r.forecast_age_s,
            "snapshot_age_s": r.snapshot_age_s,
            "signal_type": r.signal_type,
            "closures": r.closures,
            "worst_closure": r.worst_closure,
        }

    def _failure_examples(self, rows: list[RowDiagnostic], top: int) -> dict:
        final_label = f"{FOLLOW_THROUGH_HORIZONS_MINUTES[-1]}m"
        with_final = [r for r in rows if final_label in r.closures]

        largest_negative_closure = sorted(
            with_final, key=lambda r: r.closures[final_label]
        )[:top]
        # adverse move in probability points (midpoint moved against the gap)
        largest_adverse_move = sorted(
            with_final,
            key=lambda r: r.closures[final_label] * abs(r.gap or 0),
        )[:top]

        by_ticker: dict[str, list[RowDiagnostic]] = {}
        for r in with_final:
            by_ticker.setdefault(r.market_ticker, []).append(r)
        repeated_failures = sorted(
            (
                {
                    "ticker": ticker,
                    "rows": len(members),
                    "toward_rate_final": _rate(
                        sum(1 for m in members if m.closures[final_label] > 0), len(members)
                    ),
                    "mean_closure_final": _mean([m.closures[final_label] for m in members]),
                }
                for ticker, members in by_ticker.items()
                if len(members) >= 3
                and all(m.closures[final_label] <= 0 for m in members)
            ),
            key=lambda d: -d["rows"],
        )[:top]

        fresh_forecast_adverse = [
            self._row_view(r)
            for r in sorted(with_final, key=lambda r: r.closures[final_label])
            if r.forecast_age_s is not None and r.forecast_age_s < 120
            and r.closures[final_label] < 0
        ][:top]
        stale_snapshot_rows = [
            self._row_view(r)
            for r in sorted(
                (x for x in rows if (x.snapshot_age_s or 0) > ARTIFACT_SNAPSHOT_P50_SECONDS),
                key=lambda x: -(x.snapshot_age_s or 0),
            )
        ][:top]

        return {
            "largest_negative_closure": [self._row_view(r) for r in largest_negative_closure],
            "largest_adverse_move": [self._row_view(r) for r in largest_adverse_move],
            "repeated_ticker_failures": repeated_failures,
            "fresh_forecast_adverse": fresh_forecast_adverse,
            "stale_snapshot_rows": stale_snapshot_rows,
        }
