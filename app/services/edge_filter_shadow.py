"""EDGE-FILTER-001 — read-only SHADOW filters over follow-through diagnostics.

FOLLOWTHROUGH-001 diagnosed the negative follow-through as adverse selection:
~80% of watchlist gaps oppose the market's own move in the prior 10 minutes,
and those rows perform far worse than gap-follows-move rows. This module asks
the natural next measurement question WITHOUT touching anything live: *if the
watchlist had been filtered by candidate adverse-selection policies, what would
the surviving population's follow-through have looked like?*

It re-slices the SAME per-row diagnostics FOLLOWTHROUGH-001 computes
(`RowDiagnostic`: gap-vs-pre-move relation, sharp pre-move, series, per-horizon
closures/paths, spread/liquidity drift) under named shadow policies and labels
each policy with the same conservative ladder EDGE-POLICY-001 uses.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): READ-ONLY SHADOW
ANALYSIS. No live edge-precheck gate, threshold, promotion, forecaster,
MarketOps/EDGE-AUTO behavior, or flag changes — this only re-slices rows that
already exist. Follow-through is market-MOVEMENT measurement (not PnL, no
fills, no positions, no sizing, no dollar figures). A `promising_shadow` label
is a measurement-quality statement that a cohort deserves more observation —
it authorizes nothing, and any future live-gate change or MVP-005B step
requires its own explicitly-accepted milestone. Inputs are existing DB rows
only; nothing is persisted; no external call is made.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.services.edge_cohort import FOLLOW_THROUGH_HORIZONS_MINUTES
from app.services.edge_followthrough import (
    EdgeFollowthroughDiagnosticService,
    RowDiagnostic,
    _mean,
    _rate,
)

logger = logging.getLogger(__name__)

SHADOW_NOTE = (
    "Read-only SHADOW filter analysis. Every number is measured market movement "
    "over rows that already exist — nothing was filtered live, nothing changes "
    "live, and no policy label is advice or an instruction. promising_shadow "
    "means 'this cohort deserves more observation', never an authorization. "
    "Not PnL, not EV; no sizing, orders, wallets, keys, swaps, signing, or "
    "execution."
)

FINAL_HORIZON = f"{FOLLOW_THROUGH_HORIZONS_MINUTES[-1]}m"   # 60m

# Policy label ladder — same vocabulary as EDGE-POLICY-001 (measurement quality
# only; authorizes nothing). Deterministic evaluation order is documented in
# `label_policy`.
POLICY_TOO_THIN = "too_thin"
POLICY_WORSE = "worse_than_baseline"
POLICY_NEUTRAL = "neutral"
POLICY_PROMISING = "promising_shadow"
POLICY_REJECT = "reject_policy"

MIN_READABLE_FINAL_N = 12          # below this a policy is unreadable
PROMISING_MIN_FINAL_N = 30         # per EDGE-FILTER-001 spec
PROMISING_TOWARD_RATE = 0.55       # at 30m or 60m
PROMISING_MIN_CLOSURE = 0.10       # "materially positive" mean 60m closure
MAX_TICKER_SHARE = 0.34            # severe concentration guards for promising
MAX_GAME_SHARE = 0.50              # "not driven entirely by one game"
WORSE_EPSILON = 0.03               # 60m toward-rate below baseline by more than this
REJECT_SURVIVAL_RATIO = 0.10       # keeps <10% of baseline => structurally unusable
WORST_SERIES_MIN_FINAL_N = 10      # a series needs this many final rows to be 'worst'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def game_of(ticker: str) -> str:
    """The event/game portion of a Kalshi ticker (middle segment):
    KXMLBTOTAL-26JUL082210COLLAD-14 -> 26JUL082210COLLAD. Falls back to the
    whole ticker when there is no middle segment."""
    parts = (ticker or "").split("-")
    return parts[1] if len(parts) >= 2 else (ticker or "unknown")


# --- policy predicates -----------------------------------------------------------
# Each takes (row, ctx) where ctx carries data-derived facts (e.g. the worst
# series). Predicates are pure; True = the row SURVIVES the policy.


def _all(r: RowDiagnostic, ctx: dict) -> bool:
    return True


def _exclude_gap_opposes(r: RowDiagnostic, ctx: dict) -> bool:
    # excludes only rows POSITIVELY identified as opposing; unknown/no-pre-move kept
    return r.gap_move_relation != "opposes_move"


def _require_gap_follows(r: RowDiagnostic, ctx: dict) -> bool:
    return r.gap_move_relation == "follows_move"


def _exclude_sharp_pre_move(r: RowDiagnostic, ctx: dict) -> bool:
    # excludes rows with a measured sharp pre-move; unknown (no pre-tick) kept
    return not r.sharp_pre_move


def _require_no_sharp_pre_move(r: RowDiagnostic, ctx: dict) -> bool:
    # stricter: requires the pre-move to be MEASURED and not sharp
    return r.pre_move is not None and not r.sharp_pre_move


def _exclude_price_move_threshold(r: RowDiagnostic, ctx: dict) -> bool:
    return r.signal_type != "price_move_threshold"


def _exclude_spread_markets(r: RowDiagnostic, ctx: dict) -> bool:
    return r.market_type != "spread"


def _spread_only(r: RowDiagnostic, ctx: dict) -> bool:
    return r.market_type == "spread"


def _total_only(r: RowDiagnostic, ctx: dict) -> bool:
    return r.market_type == "total"


def _winner_only(r: RowDiagnostic, ctx: dict) -> bool:
    return r.market_type == "winner"


def _totals_only_no_sharp(r: RowDiagnostic, ctx: dict) -> bool:
    return r.market_type == "total" and not r.sharp_pre_move


def _follows_and_tight_spread(r: RowDiagnostic, ctx: dict) -> bool:
    return (
        r.gap_move_relation == "follows_move"
        and r.spread_cents is not None and r.spread_cents <= 2
    )


def _follows_and_high_liquidity(r: RowDiagnostic, ctx: dict) -> bool:
    return (
        r.gap_move_relation == "follows_move"
        and r.liquidity_cents is not None and r.liquidity_cents >= 1_000_000
    )


def _follows_and_persistence_gt1(r: RowDiagnostic, ctx: dict) -> bool:
    return r.gap_move_relation == "follows_move" and r.persistence > 1


def _exclude_kxmlbspread(r: RowDiagnostic, ctx: dict) -> bool:
    return r.series != "KXMLBSPREAD"


def _exclude_worst_series(r: RowDiagnostic, ctx: dict) -> bool:
    return r.series != ctx.get("worst_series")


def _follows_exclude_spreads(r: RowDiagnostic, ctx: dict) -> bool:
    return r.gap_move_relation == "follows_move" and r.market_type != "spread"


def _follows_totals_only(r: RowDiagnostic, ctx: dict) -> bool:
    return r.gap_move_relation == "follows_move" and r.market_type == "total"


# name -> predicate; order preserved in the report (baseline first).
POLICIES: tuple[tuple[str, object], ...] = (
    ("baseline_all_watchlist", _all),
    ("exclude_gap_opposes_recent_move", _exclude_gap_opposes),
    ("require_gap_follows_recent_move", _require_gap_follows),
    ("exclude_sharp_pre_move", _exclude_sharp_pre_move),
    ("require_no_sharp_pre_move", _require_no_sharp_pre_move),
    ("exclude_price_move_threshold", _exclude_price_move_threshold),
    ("exclude_spread_markets", _exclude_spread_markets),
    ("spread_only", _spread_only),
    ("total_only", _total_only),
    ("winner_only", _winner_only),
    ("totals_only_no_sharp_pre_move", _totals_only_no_sharp),
    ("gap_follows_move_and_tight_spread", _follows_and_tight_spread),
    ("gap_follows_move_and_high_liquidity", _follows_and_high_liquidity),
    ("gap_follows_move_and_persistence_gt1", _follows_and_persistence_gt1),
    ("exclude_kxmlbspread", _exclude_kxmlbspread),
    ("exclude_worst_series", _exclude_worst_series),
    ("require_gap_follows_move_exclude_spreads", _follows_exclude_spreads),
    ("require_gap_follows_move_totals_only", _follows_totals_only),
)


def worst_series(rows: list[RowDiagnostic]) -> str | None:
    """The data-derived worst series: lowest final-horizon toward-rate among
    series with >= WORST_SERIES_MIN_FINAL_N final samples. Deterministic (ties
    break alphabetically)."""
    by_series: dict[str, list[RowDiagnostic]] = {}
    for r in rows:
        if FINAL_HORIZON in r.closures:
            by_series.setdefault(r.series, []).append(r)
    candidates = []
    for series, members in by_series.items():
        if len(members) < WORST_SERIES_MIN_FINAL_N:
            continue
        toward = _rate(
            sum(1 for m in members if m.closures[FINAL_HORIZON] > 0), len(members)
        )
        candidates.append((toward, series))
    if not candidates:
        return None
    return sorted(candidates, key=lambda t: (t[0], t[1]))[0][1]


def _dist(rows: list[RowDiagnostic], key_fn) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = key_fn(r)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def summarize_policy(
    name: str, included: list[RowDiagnostic], excluded: list[RowDiagnostic],
    baseline_n: int,
) -> dict:
    """All measurement stats for one policy's surviving population."""
    final = [r for r in included if FINAL_HORIZON in r.closures]
    horizons: dict[str, dict] = {}
    for minutes in FOLLOW_THROUGH_HORIZONS_MINUTES:
        label = f"{minutes}m"
        sampled = [r for r in included if label in r.closures]
        horizons[label] = {
            "samples": len(sampled),
            "moved_toward_rate": _rate(
                sum(1 for r in sampled if r.closures[label] > 0), len(sampled)
            ),
            "mean_gap_closure_pct": _mean([r.closures[label] for r in sampled]),
        }
    continued = sum(1 for r in final if r.paths.get(FINAL_HORIZON) == "continued_away")
    reverted = sum(1 for r in final if r.paths.get(FINAL_HORIZON) == "reverted_toward")
    flat = sum(1 for r in final if r.paths.get(FINAL_HORIZON) == "flat")
    ticker_dist = _dist(final, lambda r: r.market_ticker)
    game_dist = _dist(final, lambda r: game_of(r.market_ticker))
    return {
        "name": name,
        "included": len(included),
        "excluded": len(excluded),
        "survival_ratio": _rate(len(included), baseline_n),
        "final_n": len(final),
        "market_type_mix": _dist(included, lambda r: r.market_type),
        "signal_type_mix": _dist(included, lambda r: r.signal_type),
        "series_mix": _dist(included, lambda r: r.series),
        "gap_sign_mix": _dist(
            included, lambda r: "positive" if (r.gap or 0) >= 0 else "negative"
        ),
        "follow_through": horizons,
        "continued_away_rate": _rate(continued, len(final)),
        "reverted_toward_rate": _rate(reverted, len(final)),
        "flat_rate": _rate(flat, len(final)),
        "spread_change_60m_mean": _mean([r.spread_change_60m for r in included]),
        "liquidity_change_60m_mean": _mean([r.liquidity_change_60m for r in included]),
        "max_ticker_share": (
            _rate(max(ticker_dist.values()), len(final)) if ticker_dist else None
        ),
        "max_game_share": (
            _rate(max(game_dist.values()), len(final)) if game_dist else None
        ),
    }


def label_policy(summary: dict, baseline: dict) -> tuple[str, str]:
    """Conservative deterministic label. Evaluation order (documented):
    1. promising_shadow — final_n >= 30 AND (toward>=0.55 at 30m or 60m OR mean
       60m closure >= +0.10) AND no severe single-ticker/-game concentration;
    2. promising-but-concentrated (n >= 30, bar cleared, concentration fails)
       — neutral, with the concentration called out;
    3. bar-clearing but UNDER-SAMPLED (12 <= n < 30, rates/closure clear the
       promising bar) — too_thin with a keep-observing reason. A selective
       filter that is merely young is NOT structurally unusable, so this is
       checked BEFORE the survival-based reject;
    4. too_thin — final_n < 12 (unreadable);
    5. reject_policy — keeps < 10% of the baseline population without clearing
       any bar (structurally too narrow to ever produce a usable population);
    6. worse_than_baseline — 60m toward-rate more than 0.03 below baseline;
    7. neutral — everything else. Labels are measurement quality; never advice."""
    n = summary["final_n"]
    ft = summary["follow_through"]
    toward_30 = ft.get("30m", {}).get("moved_toward_rate")
    toward_60 = ft.get("60m", {}).get("moved_toward_rate")
    closure_60 = ft.get("60m", {}).get("mean_gap_closure_pct")
    baseline_60 = baseline["follow_through"].get("60m", {}).get("moved_toward_rate")

    rate_ok = (toward_30 or 0) >= PROMISING_TOWARD_RATE or (
        (toward_60 or 0) >= PROMISING_TOWARD_RATE
    )
    closure_ok = closure_60 is not None and closure_60 >= PROMISING_MIN_CLOSURE
    concentration_ok = (
        (summary["max_ticker_share"] or 0) <= MAX_TICKER_SHARE
        and (summary["max_game_share"] or 0) <= MAX_GAME_SHARE
    )

    if n >= PROMISING_MIN_FINAL_N and (rate_ok or closure_ok):
        if concentration_ok:
            return POLICY_PROMISING, (
                f"final_n={n}, toward 30m={toward_30} 60m={toward_60}, "
                f"closure_60m={closure_60}, concentration ok — deserves more "
                f"observation (authorizes nothing)"
            )
        return POLICY_NEUTRAL, (
            f"rates clear the bar but concentration fails "
            f"(max_ticker_share={summary['max_ticker_share']}, "
            f"max_game_share={summary['max_game_share']}) — one game/ticker "
            f"could be driving it"
        )
    if MIN_READABLE_FINAL_N <= n < PROMISING_MIN_FINAL_N and (rate_ok or closure_ok):
        return POLICY_TOO_THIN, (
            f"clears the promising bar (toward 30m={toward_30} 60m={toward_60}, "
            f"closure_60m={closure_60}) but final_n={n} < {PROMISING_MIN_FINAL_N} — "
            f"a selective filter that is young, not unusable; keep observing"
        )
    if n < MIN_READABLE_FINAL_N:
        return POLICY_TOO_THIN, f"final_n={n} < {MIN_READABLE_FINAL_N}"
    if (summary["survival_ratio"] or 0) < REJECT_SURVIVAL_RATIO:
        return POLICY_REJECT, (
            f"keeps only {summary['survival_ratio']:.0%} of the baseline without "
            f"clearing any bar — too narrow to ever produce a usable measurement "
            f"population"
        )
    if (
        baseline_60 is not None and toward_60 is not None
        and toward_60 < baseline_60 - WORSE_EPSILON
    ):
        return POLICY_WORSE, (
            f"60m toward {toward_60} vs baseline {baseline_60} — the filter keeps "
            f"the worse rows"
        )
    return POLICY_NEUTRAL, (
        f"60m toward {toward_60} vs baseline {baseline_60} — no material shadow improvement"
    )


class EdgeFilterShadowReportService:
    """Builds the shadow-filter report from FOLLOWTHROUGH-001 row diagnostics.
    Read-only; persists nothing; changes nothing."""

    def build(self, session: Session, hours: int = 24, top: int = 5) -> dict:
        rows = EdgeFollowthroughDiagnosticService().build_row_diagnostics(session, hours)
        ctx = {"worst_series": worst_series(rows)}
        baseline_n = len(rows)

        results: list[dict] = []
        baseline_summary: dict | None = None
        for name, predicate in POLICIES:
            included = [r for r in rows if predicate(r, ctx)]
            excluded = [r for r in rows if not predicate(r, ctx)]
            summary = summarize_policy(name, included, excluded, baseline_n)
            if name == "baseline_all_watchlist":
                baseline_summary = summary
                summary["label"], summary["label_reason"] = POLICY_NEUTRAL, "baseline"
            else:
                summary["label"], summary["label_reason"] = label_policy(
                    summary, baseline_summary
                )
            summary["examples_removed"] = self._examples(excluded, top, worst=True)
            summary["examples_retained"] = self._examples(included, top, worst=False)
            results.append(summary)

        return {
            "note": SHADOW_NOTE,
            "window_hours": hours,
            "population": baseline_n,
            "worst_series": ctx["worst_series"],
            "policies": results,
            "interpretation": self._interpret(results),
        }

    @staticmethod
    def _examples(rows: list[RowDiagnostic], top: int, worst: bool) -> list[dict]:
        """Concrete rows a policy removed (worst first) or retained (best
        first). Measurement views only."""
        with_final = [r for r in rows if FINAL_HORIZON in r.closures]
        ordered = sorted(
            with_final,
            key=lambda r: r.closures[FINAL_HORIZON],
            reverse=not worst,
        )
        return [
            {
                "ticker": r.market_ticker,
                "gap": r.gap,
                "relation": r.gap_move_relation,
                "closure_60m": r.closures[FINAL_HORIZON],
            }
            for r in ordered[:top]
        ]

    @staticmethod
    def _interpret(results: list[dict]) -> dict:
        """Programmatic answers to the EDGE-FILTER-001 questions, from the
        computed numbers only. Interpretation of measurement — never advice."""
        by_name = {r["name"]: r for r in results}

        def t60(name: str):
            return by_name.get(name, {}).get("follow_through", {}).get("60m", {})

        baseline = t60("baseline_all_watchlist")
        excl_opposes = t60("exclude_gap_opposes_recent_move")
        req_follows = t60("require_gap_follows_recent_move")
        spread = t60("spread_only")
        excl_spread = t60("exclude_spread_markets")
        total = t60("total_only")

        def delta(a: dict, b: dict) -> float | None:
            if a.get("moved_toward_rate") is None or b.get("moved_toward_rate") is None:
                return None
            return round(a["moved_toward_rate"] - b["moved_toward_rate"], 4)

        promising = [r["name"] for r in results if r["label"] == POLICY_PROMISING]
        clears_mvp = [
            r["name"] for r in results
            if r["final_n"] >= 20
            and (t60(r["name"]).get("moved_toward_rate") or 0) >= 0.55
        ]
        return {
            "excluding_gap_opposes_improves": {
                "toward_60m_delta_vs_baseline": delta(excl_opposes, baseline),
                "closure_60m": excl_opposes.get("mean_gap_closure_pct"),
            },
            "requiring_gap_follows_helps_enough": {
                "toward_60m": req_follows.get("moved_toward_rate"),
                "clears_promising_bar": (req_follows.get("moved_toward_rate") or 0)
                >= PROMISING_TOWARD_RATE,
            },
            "spreads_primary_adverse_source": {
                "spread_only_toward_60m": spread.get("moved_toward_rate"),
                "exclude_spreads_toward_60m": excl_spread.get("moved_toward_rate"),
                "delta_exclude_vs_baseline": delta(excl_spread, baseline),
            },
            "totals_meaningfully_less_bad": {
                "total_only_toward_60m": total.get("moved_toward_rate"),
                "delta_vs_baseline": delta(total, baseline),
            },
            "cohorts_worth_future_live_gate_consideration": promising,
            "mvp_005b": {
                "policies_clearing_mvp_bar": clears_mvp,
                "blocked": not clears_mvp,
                "note": (
                    "MVP-005B remains gated on explicit human acceptance regardless "
                    "of any shadow result; a promising_shadow label only motivates "
                    "more observation."
                ),
            },
        }
