"""TRIGGER-TIMING-001 — read-only SHADOW simulation of measurement timing.

FORECAST-ANCHOR-001's verdict was `timing_adverse_selection`: the
price_move_threshold trigger measures gaps exactly when the market is trending,
and the trend then continues — anchoring is secondary. This module tests the
natural follow-up WITHOUT touching anything live: *if edge-precheck had
measured LATER (a fixed cooldown, or after the market settled), what would the
gap and its follow-through have looked like?*

For each historical watchlist row it replays alternate measurement times over
the SAME persisted ticks: the recorded forecast is held fixed (the honest
simulation of "same trigger, later measurement"), the gap is re-derived from
the midpoint at the delayed time, rows whose gap fell below the live
min-abs-gap are counted as GAP_EVAPORATED (mean reversion happened before
measurement — itself evidence), and follow-through horizons are measured from
the delayed time. Conditional policies (midpoint flat for 5m, spread stable,
gap-follows-move) search bounded windows over the same tick series.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): READ-ONLY SHADOW
ANALYSIS. No live trigger, gate, threshold, forecast, promotion, flag, or
MarketOps/EDGE-AUTO change — this replays rows that already exist. All numbers
are measured market movement — not PnL, no fills, no positions, never advice.
A promising_shadow label motivates observation only; any live change is a
separate explicitly-accepted milestone. Nothing is persisted; no external call.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MarketPriceTick
from app.services.edge_cohort import FOLLOW_THROUGH_HORIZONS_MINUTES
from app.services.edge_filter_shadow import (
    POLICY_NEUTRAL,
    game_of,
    label_policy,
)
from app.services.edge_followthrough import (
    EdgeFollowthroughDiagnosticService,
    RowDiagnostic,
    _aware,
    _mean,
    _rate,
)

logger = logging.getLogger(__name__)

TIMING_NOTE = (
    "Read-only SHADOW timing simulation. Alternate measurement times are "
    "replayed over ticks that already exist; the recorded forecast is held "
    "fixed; nothing was measured differently live and nothing changes live. "
    "Every number is measured market movement — not PnL, not EV, never advice; "
    "labels motivate observation only. No sizing, orders, wallets, keys, "
    "swaps, signing, or execution."
)

MIN_ABS_GAP = 0.05          # mirrors the live edge-precheck min-abs-gap gate
PRE_MOVE_WINDOW_MIN = 10    # same window FOLLOWTHROUGH-001 uses
SHARP_PRE_MOVE = 0.03
FLAT_BAND = 0.01            # midpoint band for "flat"
STABLE_WINDOW_MIN = 5       # flat/stable window length
MAX_WAIT_MIN = 30           # conditional policies give up after this
FINAL_HORIZON = f"{FOLLOW_THROUGH_HORIZONS_MINUTES[-1]}m"

LOSS_NO_TICK = "no_tick_at_delay"
LOSS_GAP_EVAPORATED = "gap_evaporated"
LOSS_CONDITION_NEVER_MET = "condition_never_met"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Tick:
    at: datetime
    mid: float
    spread: int | None
    liquidity: int | None


class TickSeries:
    """One row's preloaded tick series (sorted). Pure-Python lookups so every
    timing policy is deterministic and unit-testable without a DB."""

    def __init__(self, ticks: list[Tick]):
        self.ticks = sorted(ticks, key=lambda t: t.at)

    def last_at_or_before(self, ts: datetime) -> Tick | None:
        best = None
        for t in self.ticks:
            if t.at <= ts:
                best = t
            else:
                break
        return best

    def in_range(self, start: datetime, end: datetime) -> list[Tick]:
        return [t for t in self.ticks if start <= t.at <= end]

    def first_flat_end(
        self, start: datetime, band: float, window_min: int, max_wait_min: int
    ) -> datetime | None:
        """Earliest time t*+window such that every tick in [t*, t*+window] stays
        within ±band of the window's first tick, anchored at ticks after
        `start`, giving up past `max_wait_min`. Requires >=2 ticks in the window
        so 'flat' is evidenced, not vacuous."""
        deadline = start + timedelta(minutes=max_wait_min)
        candidates = [t for t in self.ticks if start < t.at <= deadline]
        for anchor in candidates:
            window_end = anchor.at + timedelta(minutes=window_min)
            if window_end > deadline + timedelta(minutes=window_min):
                break
            window = self.in_range(anchor.at, window_end)
            if len(window) < 2:
                continue
            if all(abs(t.mid - anchor.mid) <= band for t in window):
                return window_end
        return None

    def first_spread_stable_end(
        self, start: datetime, window_min: int, max_wait_min: int
    ) -> datetime | None:
        """Earliest time t*+window where every tick in [t*, t*+window] has the
        SAME spread (>=2 ticks). Same bounded search as first_flat_end."""
        deadline = start + timedelta(minutes=max_wait_min)
        candidates = [t for t in self.ticks if start < t.at <= deadline]
        for anchor in candidates:
            if anchor.spread is None:
                continue
            window_end = anchor.at + timedelta(minutes=window_min)
            if window_end > deadline + timedelta(minutes=window_min):
                break
            window = self.in_range(anchor.at, window_end)
            if len(window) < 2:
                continue
            if all(t.spread == anchor.spread for t in window):
                return window_end
        return None


@dataclass
class SimulatedMeasurement:
    """One row measured at an alternate time. Measurements only."""

    row: RowDiagnostic
    measured_at: datetime
    delay_s: float
    midpoint: float
    gap: float
    pre_move: float | None          # 10m before the DELAYED time
    sharp_pre_move: bool = False
    gap_opposes_move: bool | None = None
    closures: dict = field(default_factory=dict)   # horizon -> closure from delayed time
    paths: dict = field(default_factory=dict)
    spread_change_60m: int | None = None
    liquidity_change_60m: int | None = None


@dataclass
class PolicyOutcome:
    name: str
    measurements: list[SimulatedMeasurement] = field(default_factory=list)
    losses: dict = field(default_factory=dict)      # reason -> count

    def lost(self, reason: str) -> None:
        self.losses[reason] = self.losses.get(reason, 0) + 1


def measure_at(
    row: RowDiagnostic, series: TickSeries, at: datetime
) -> SimulatedMeasurement | str:
    """Simulate measuring `row` at time `at` over its tick series. Returns the
    measurement, or a loss reason. The recorded forecast probability is held
    fixed; the gap is re-derived from the midpoint at `at`; a delayed gap below
    the live min-abs-gap means the row would not have been measured
    (GAP_EVAPORATED — mean reversion beat the measurement)."""
    if row.forecast_probability is None:
        return LOSS_NO_TICK
    created = _aware(row.created_at)
    tick = None
    if at > created:
        # the measurement tick must postdate the trigger — never reuse the
        # pre-trigger book for a delayed measurement
        window = series.in_range(created + timedelta(microseconds=1), at)
        tick = window[-1] if window else None
    else:
        tick = series.last_at_or_before(at)
    if tick is None:
        return LOSS_NO_TICK
    gap = round(row.forecast_probability - tick.mid, 4)
    if abs(gap) < MIN_ABS_GAP:
        return LOSS_GAP_EVAPORATED

    pre_tick_window = series.in_range(at - timedelta(minutes=PRE_MOVE_WINDOW_MIN), at)
    pre_move = round(tick.mid - pre_tick_window[0].mid, 4) if pre_tick_window else None
    opposes = None
    if pre_move is not None and abs(pre_move) > 1e-9:
        opposes = (gap > 0) != (pre_move > 0)

    sim = SimulatedMeasurement(
        row=row, measured_at=at,
        delay_s=round((at - created).total_seconds(), 1),
        midpoint=tick.mid, gap=gap, pre_move=pre_move,
        sharp_pre_move=(pre_move is not None and abs(pre_move) >= SHARP_PRE_MOVE),
        gap_opposes_move=opposes,
    )
    for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
        deadline = at + timedelta(minutes=minutes)
        later = series.in_range(at + timedelta(microseconds=1), deadline)
        if not later:
            sim.paths[f"{minutes}m"] = "no_sample"
            continue
        last = later[-1]
        closure = round((last.mid - tick.mid) / gap, 4)
        sim.closures[f"{minutes}m"] = closure
        sim.paths[f"{minutes}m"] = (
            "continued_away" if closure <= -0.25
            else "reverted_toward" if closure >= 0.25
            else "flat"
        )
        if minutes == FOLLOW_THROUGH_HORIZONS_MINUTES[-1]:
            if last.spread is not None and tick.spread is not None:
                sim.spread_change_60m = last.spread - tick.spread
            if last.liquidity is not None and tick.liquidity is not None:
                sim.liquidity_change_60m = last.liquidity - tick.liquidity
    return sim


class TriggerTimingShadowReportService:
    """Builds the timing-shadow report. Read-only; persists nothing."""

    def _series_for(self, session: Session, row: RowDiagnostic) -> TickSeries:
        created = _aware(row.created_at)
        rows = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == row.market_ticker,
                MarketPriceTick.observed_at >= created - timedelta(minutes=PRE_MOVE_WINDOW_MIN + MAX_WAIT_MIN),
                MarketPriceTick.observed_at <= created + timedelta(
                    minutes=MAX_WAIT_MIN + STABLE_WINDOW_MIN + FOLLOW_THROUGH_HORIZONS_MINUTES[-1] + 5
                ),
                MarketPriceTick.midpoint.is_not(None),
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().all()
        return TickSeries([
            Tick(at=_aware(t.observed_at), mid=t.midpoint, spread=t.spread,
                 liquidity=t.liquidity_proxy)
            for t in rows
        ])

    # --- timing policies -------------------------------------------------------

    def _apply_policy(
        self, name: str, row: RowDiagnostic, series: TickSeries
    ) -> SimulatedMeasurement | str:
        created = _aware(row.created_at)
        if name == "baseline_immediate":
            return measure_at(row, series, created)
        if name.startswith("delay_"):
            minutes = int(name.split("_")[1].rstrip("m"))
            return measure_at(row, series, created + timedelta(minutes=minutes))
        if name == "wait_until_midpoint_flat_5m":
            end = series.first_flat_end(created, FLAT_BAND, STABLE_WINDOW_MIN, MAX_WAIT_MIN)
            if end is None:
                return LOSS_CONDITION_NEVER_MET
            return measure_at(row, series, end)
        if name == "wait_until_spread_stable":
            end = series.first_spread_stable_end(created, STABLE_WINDOW_MIN, MAX_WAIT_MIN)
            if end is None:
                return LOSS_CONDITION_NEVER_MET
            return measure_at(row, series, end)
        if name == "wait_until_gap_follows_move":
            # earliest post-trigger tick where the re-derived gap FOLLOWS the
            # 10m pre-move at that time (and still clears the min gap)
            deadline = created + timedelta(minutes=MAX_WAIT_MIN)
            for t in series.in_range(created + timedelta(microseconds=1), deadline):
                candidate = measure_at(row, series, t.at)
                if isinstance(candidate, str):
                    continue
                if candidate.gap_opposes_move is False:
                    return candidate
            return LOSS_CONDITION_NEVER_MET
        raise ValueError(f"unknown timing policy {name}")

    POLICY_NAMES = (
        "baseline_immediate",
        "delay_2m",
        "delay_5m",
        "delay_10m",
        "delay_15m",
        "wait_until_midpoint_flat_5m",
        "wait_until_spread_stable",
        "wait_until_gap_follows_move",
    )

    # --- aggregation ------------------------------------------------------------

    @staticmethod
    def _summarize(outcome: PolicyOutcome, baseline_rows: int) -> dict:
        sims = outcome.measurements
        final = [s for s in sims if FINAL_HORIZON in s.closures]
        horizons: dict[str, dict] = {}
        for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
            label = f"{minutes}m"
            sampled = [s for s in sims if label in s.closures]
            horizons[label] = {
                "samples": len(sampled),
                "moved_toward_rate": _rate(
                    sum(1 for s in sampled if s.closures[label] > 0), len(sampled)
                ),
                "mean_gap_closure_pct": _mean([s.closures[label] for s in sampled]),
            }
        opposes_known = [s for s in sims if s.gap_opposes_move is not None]
        continued = sum(1 for s in final if s.paths.get(FINAL_HORIZON) == "continued_away")
        reverted = sum(1 for s in final if s.paths.get(FINAL_HORIZON) == "reverted_toward")
        flat = sum(1 for s in final if s.paths.get(FINAL_HORIZON) == "flat")

        def dist(key_fn):
            out: dict[str, int] = {}
            for s in sims:
                k = key_fn(s)
                out[k] = out.get(k, 0) + 1
            return dict(sorted(out.items(), key=lambda kv: -kv[1]))

        ticker_dist = dist(lambda s: s.row.market_ticker)
        game_dist = dist(lambda s: game_of(s.row.market_ticker))
        return {
            "name": outcome.name,
            "rows_measurable": len(sims),
            "final_n": len(final),
            "rows_lost": dict(sorted(outcome.losses.items(), key=lambda kv: -kv[1])),
            "survival_ratio": _rate(len(sims), baseline_rows),
            "median_delay_s": (
                sorted(s.delay_s for s in sims)[len(sims) // 2] if sims else None
            ),
            "market_type_mix": dist(lambda s: s.row.market_type),
            "signal_type_mix": dist(lambda s: s.row.signal_type),
            "gap_sign_mix": dist(lambda s: "positive" if s.gap >= 0 else "negative"),
            "sharp_pre_move_share": _rate(
                sum(1 for s in sims if s.sharp_pre_move), len(sims)
            ),
            "gap_opposes_move_share": _rate(
                sum(1 for s in opposes_known if s.gap_opposes_move), len(opposes_known)
            ),
            "follow_through": horizons,
            "continued_away_rate": _rate(continued, len(final)),
            "reverted_toward_rate": _rate(reverted, len(final)),
            "flat_rate": _rate(flat, len(final)),
            "spread_change_60m_mean": _mean([s.spread_change_60m for s in sims]),
            "liquidity_change_60m_mean": _mean([s.liquidity_change_60m for s in sims]),
            "max_ticker_share": (
                _rate(max(ticker_dist.values()), len(final)) if ticker_dist and final else None
            ),
            "max_game_share": (
                _rate(max(game_dist.values()), len(final)) if game_dist and final else None
            ),
        }

    def build(self, session: Session, hours: int = 24, top: int = 5) -> dict:
        rows = EdgeFollowthroughDiagnosticService().build_row_diagnostics(session, hours)
        series_by_id = {r.snapshot_id: self._series_for(session, r) for r in rows}

        outcomes: dict[str, PolicyOutcome] = {}
        for name in self.POLICY_NAMES:
            outcome = PolicyOutcome(name=name)
            for r in rows:
                result = self._apply_policy(name, r, series_by_id[r.snapshot_id])
                if isinstance(result, str):
                    outcome.lost(result)
                else:
                    outcome.measurements.append(result)
            outcomes[name] = outcome

        baseline_summary = self._summarize(outcomes["baseline_immediate"], len(rows))
        baseline_summary["label"], baseline_summary["label_reason"] = (
            POLICY_NEUTRAL, "baseline",
        )
        summaries = [baseline_summary]
        for name in self.POLICY_NAMES[1:]:
            s = self._summarize(outcomes[name], len(rows))
            s["label"], s["label_reason"] = label_policy(s, baseline_summary)
            summaries.append(s)

        return {
            "note": TIMING_NOTE,
            "window_hours": hours,
            "population": len(rows),
            "policies": summaries,
            "examples": self._examples(outcomes, top),
            "comparison": self._compare(summaries),
        }

    # --- examples -----------------------------------------------------------------

    @staticmethod
    def _examples(outcomes: dict[str, PolicyOutcome], top: int) -> dict:
        """Rows a delay IMPROVED or WORSENED most vs the immediate baseline
        (same snapshot, 60m closure delta), shown for the 10m delay policy."""
        base = {
            s.row.snapshot_id: s
            for s in outcomes["baseline_immediate"].measurements
            if FINAL_HORIZON in s.closures
        }
        delayed = [
            s for s in outcomes["delay_10m"].measurements
            if FINAL_HORIZON in s.closures and s.row.snapshot_id in base
        ]
        deltas = [
            {
                "ticker": s.row.market_ticker,
                "baseline_closure_60m": base[s.row.snapshot_id].closures[FINAL_HORIZON],
                "delayed_closure_60m": s.closures[FINAL_HORIZON],
                "delta": round(
                    s.closures[FINAL_HORIZON]
                    - base[s.row.snapshot_id].closures[FINAL_HORIZON], 4
                ),
                "gap_at_delay": s.gap,
            }
            for s in delayed
        ]
        improved = sorted(deltas, key=lambda d: -d["delta"])[:top]
        worsened = sorted(deltas, key=lambda d: d["delta"])[:top]
        return {"improved_by_delay_10m": improved, "worsened_by_delay_10m": worsened}

    # --- comparison ------------------------------------------------------------------

    @staticmethod
    def _compare(summaries: list[dict]) -> dict:
        """Programmatic answers to the TRIGGER-TIMING-001 questions.
        Measurement only — never advice."""
        by_name = {s["name"]: s for s in summaries}
        base = by_name["baseline_immediate"]

        def t60(s):
            return s["follow_through"].get("60m", {})

        delay_rowset = ["delay_2m", "delay_5m", "delay_10m", "delay_15m"]
        opposes_trend = {
            name: by_name[name]["gap_opposes_move_share"] for name in
            ["baseline_immediate"] + delay_rowset
        }
        closure_trend = {
            name: t60(by_name[name]).get("mean_gap_closure_pct")
            for name in ["baseline_immediate"] + delay_rowset
        }
        survival_trend = {
            name: by_name[name]["survival_ratio"] for name in delay_rowset
        }
        evaporated = {
            name: (by_name[name]["rows_lost"] or {}).get(LOSS_GAP_EVAPORATED, 0)
            for name in delay_rowset
        }

        # totals vs spreads under the 10m delay
        d10 = by_name["delay_10m"]
        cooldown_best = max(
            (s for s in summaries if s["name"].startswith("delay_")),
            key=lambda s: (t60(s).get("mean_gap_closure_pct") or -99),
        )
        follows_wait = by_name["wait_until_gap_follows_move"]

        return {
            "does_delay_reduce_gap_opposes_share": opposes_trend,
            "does_delay_improve_closure": closure_trend,
            "delay_survival_and_gap_evaporation": {
                "survival_ratio": survival_trend,
                "gap_evaporated_rows": evaporated,
                "note": (
                    "gap evaporation during a delay IS mean reversion arriving "
                    "before measurement — high evaporation with better surviving "
                    "closure means the delay is filtering exactly the reverting rows"
                ),
            },
            "delay_10m_market_type_mix": d10["market_type_mix"],
            "best_cooldown_policy": {
                "name": cooldown_best["name"],
                "closure_60m": t60(cooldown_best).get("mean_gap_closure_pct"),
                "toward_60m": t60(cooldown_best).get("moved_toward_rate"),
                "label": cooldown_best["label"],
            },
            "cooldown_vs_condition_filter": {
                "best_cooldown_closure_60m": t60(cooldown_best).get("mean_gap_closure_pct"),
                "wait_gap_follows_closure_60m": t60(follows_wait).get("mean_gap_closure_pct"),
                "wait_gap_follows_survival": follows_wait["survival_ratio"],
                "note": (
                    "a condition-wait keeps only rows where the gap eventually "
                    "follows the move; a cooldown keeps every row that still has a "
                    "gap — compare closure AND survival, not closure alone"
                ),
            },
            "caveat": (
                "the recorded forecast is held fixed in this simulation; live "
                "behavior could differ if the forecaster refreshes during the "
                "delay. Any live change remains a separate explicitly-accepted "
                "milestone."
            ),
        }
