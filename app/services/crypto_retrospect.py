"""CRYPTO-RETROSPECT-001/002 — read-only retrospective feature/outcome analysis.

Answers ONE evidence-building question: which observable memecoin features
(holder concentration, risk labels/reasons, liquidity depth, volume shape,
boost/attention, social metadata, launch venue, provider coverage, missing
data) actually SEPARATE the CRYPTO-TAPE-001 survival outcomes (survived_*,
liquidity_removed, dead_volume, severe_risk, graduated_or_migrated,
provider_gap)?

Compute-on-demand, exactly like MEME-SHADOW: it PERSISTS NOTHING, makes ZERO
external calls, and has ZERO provider-budget impact. It composes the
CRYPTO-TAPE-001 recorder's pure builders over the recent token universe —
persisted tape birth events are preferred as anchors when they exist; other
tokens get an on-the-fly (never persisted) derivation from the same
already-persisted rows. Cohorts below the sample floor are labeled
`too_thin`; immature or unmeasurable outcomes stay unknown and are excluded
from rates — nothing is guessed.

CRYPTO-RETROSPECT-002 adds TAPE-BACKED COHORT STRATIFICATION: because fresh
derived-only tokens (just discovered, horizons immature) dilute the window and
can manufacture apparent patterns, the report now separates mature tape-backed
evidence (a persisted birth event with repeated re-observations) from
derived-only noise. `--cohort {all,tape-backed,derived-only}` re-lenses the
headline; a `data_source_mix` section and a per-dimension `source_stratification`
(all vs tape-backed vs derived-only, with a dilution warning + a source label)
tell you WHERE any apparent signal actually lives.

Hard boundary (AGENTS.md, docs/SAFETY_BOUNDARIES.md): MEASUREMENT only. A
separation label is a statement about label/feature quality — never PnL, EV,
a return, a side, a size, or a recommendation. `strong_risk_separator` means
"this feature separates measured risk outcomes; weigh it in review triage"
— never buy/sell/avoid-as-trade-direction. No wallets, keys, swaps,
signing, orders, execution, or autonomy.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CryptoToken, CryptoTokenBirthEvent
from app.services.crypto_risk_engine import RiskEngineConfig, level_for
from app.services.crypto_tape import (
    CryptoLifecycleTapeRecorder,
    merged_assessment_flags,
)

logger = logging.getLogger(__name__)

RETROSPECT_NOTE = (
    "Read-only retrospective MEASUREMENT: which persisted features separate "
    "the lifecycle-tape survival outcomes? Derived on demand from "
    "already-persisted rows — nothing persisted, no external call, no "
    "provider-budget impact. A separation label describes feature/label "
    "quality for review triage — never PnL, EV, a side, a size, or a "
    "recommendation. No wallets, keys, swaps, signing, orders, or execution."
)

# sample floors (mirror MEME-SHADOW conservatism)
MIN_COHORT_SAMPLES = 12      # cohort below this is too_thin
MIN_MEASURABLE = 6           # rate needs at least this many measured outcomes
# separation thresholds on best-vs-worst cohort rate delta
SEPARATION_WEAK = 0.10
SEPARATION_STRONG = 0.25
# a dimension whose primary outcomes are mostly unmeasurable is gap-dominated
MEASURABILITY_FLOOR = 0.4
# hard cap on the analysis universe (manual report on a shared host)
MAX_TOKENS = 400

SURVIVAL_OUTCOMES = ("survived_15m", "survived_1h", "survived_6h", "survived_24h")
RISK_OUTCOMES = ("liquidity_removed", "dead_volume", "severe_risk")
ALL_OUTCOMES = SURVIVAL_OUTCOMES + RISK_OUTCOMES + (
    "graduated_or_migrated", "provider_gap",
)

LABEL_TOO_THIN = "too_thin"
LABEL_GAP_DOMINATED = "provider_gap_dominates"
LABEL_NO_SEPARATION = "no_separation"
LABEL_WEAK = "weak_separator"
LABEL_STRONG_RISK = "strong_risk_separator"
LABEL_STRONG_SURVIVAL = "strong_survival_separator"

# labels that mean a dimension produced an actual separator signal
SIGNAL_LABELS = frozenset({LABEL_WEAK, LABEL_STRONG_RISK, LABEL_STRONG_SURVIVAL})

# CRYPTO-RETROSPECT-002: per-dimension source-comparison labels (tape-backed
# vs derived-only). These describe WHERE any apparent signal lives, never a
# trade direction.
SOURCE_TAPE_TOO_THIN = "tape_too_thin"            # tape-backed sample can't be read
SOURCE_TAPE_READABLE = "tape_readable"            # tape readable, no strong signal
SOURCE_ALL_DILUTED = "all_window_diluted"         # derived rows wash out / distort
SOURCE_DERIVED_DOMINATES = "derived_only_dominates"  # signal comes from fresh tokens
SOURCE_CONSISTENT = "consistent_across_sources"   # tape + derived agree
SOURCE_TAPE_ONLY_HINT = "tape_only_hint"          # signal only in tape; can't cross-check

# valid --cohort selections
COHORT_ALL = "all"
COHORT_TAPE_BACKED = "tape-backed"
COHORT_DERIVED_ONLY = "derived-only"
COHORT_CHOICES = (COHORT_ALL, COHORT_TAPE_BACKED, COHORT_DERIVED_ONLY)

# concentration thresholds anchored to the risk engine's defaults
_ENGINE_DEFAULTS = RiskEngineConfig()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- pure bucket functions ------------------------------------------------------


def bucket_concentration(value: float | None, threshold: float) -> str:
    """absent / low / elevated (>= half threshold) / flagged (>= threshold)."""
    if value is None:
        return "absent"
    if value >= threshold:
        return "flagged"
    if value >= threshold / 2:
        return "elevated"
    return "low"


def bucket_liquidity(value: float | None) -> str:
    if value is None:
        return "absent"
    if value < 5_000:
        return "<5k"
    if value < 25_000:
        return "5k-25k"
    if value < 100_000:
        return "25k-100k"
    return ">=100k"


def bucket_volume_to_liquidity(value: float | None) -> str:
    """Mirrors the risk engine's manipulation heuristics: >=20x smells painted."""
    if value is None:
        return "absent"
    if value < 0.5:
        return "quiet(<0.5x)"
    if value < 2:
        return "active(0.5-2x)"
    if value < 20:
        return "hot(2-20x)"
    return "suspect(>=20x)"


def bucket_attention(value: float | None) -> str:
    if value is None:
        return "absent"
    if value >= 0.7:
        return "high(>=0.7)"
    if value >= 0.4:
        return "mid(0.4-0.7)"
    return "low(<0.4)"


def bucket_boost(boost_amount: float | None) -> str:
    if boost_amount is None:
        return "absent"
    return "boosted" if boost_amount > 0 else "not_boosted"


def bucket_social(has_social) -> str:
    if has_social is None:
        return "unknown"
    return "social_present" if has_social else "social_missing"


def bucket_risk_score(value: float | None) -> str:
    return level_for(value)  # low/medium/high/severe/unknown


# --- per-token feature/outcome row ----------------------------------------------


@dataclass
class FeatureOutcomeRow:
    token_address: str
    symbol: str | None
    tape_backed: bool                      # persisted birth event existed
    buckets: dict = field(default_factory=dict)       # dimension -> cohort name
    risk_reasons: list = field(default_factory=list)  # multi-membership
    missing_info: list = field(default_factory=list)  # multi-membership
    outcomes: dict = field(default_factory=dict)      # outcome -> True|False|None


class CryptoRetrospectService:
    """Joins features to survival outcomes over the recent token universe.
    Session-only; composes the tape recorder's pure builders; persists
    nothing (no row is ever added to the session)."""

    def __init__(self, recorder: CryptoLifecycleTapeRecorder | None = None):
        self.recorder = recorder or CryptoLifecycleTapeRecorder()

    def _universe(self, session: Session, hours: int) -> tuple[list[CryptoToken], bool]:
        cutoff = _now() - timedelta(hours=hours)
        tokens = list(session.execute(
            select(CryptoToken)
            .where(
                CryptoToken.chain == self.recorder.config.chain,
                CryptoToken.first_seen_at >= cutoff,
            )
            .order_by(CryptoToken.first_seen_at.desc(), CryptoToken.id.desc())
            .limit(MAX_TOKENS + 1)
        ).scalars().all())
        truncated = len(tokens) > MAX_TOKENS
        return tokens[:MAX_TOKENS], truncated

    def rows(self, session: Session, hours: int = 48) -> tuple[list[FeatureOutcomeRow], bool]:
        now = _now()
        tokens, truncated = self._universe(session, hours)
        births = {
            b.token_address: b
            for b in session.execute(
                select(CryptoTokenBirthEvent).where(
                    CryptoTokenBirthEvent.chain == self.recorder.config.chain,
                    CryptoTokenBirthEvent.token_address.in_(
                        [t.token_address for t in tokens]
                    ),
                )
            ).scalars().all()
        } if tokens else {}

        results: list[FeatureOutcomeRow] = []
        cfg = _ENGINE_DEFAULTS
        for token in tokens:
            sources = self.recorder._load_sources(session, token, now)
            birth = births.get(token.token_address)
            tape_backed = birth is not None
            if birth is None:
                # on-the-fly derivation from the same persisted rows —
                # constructed only, NEVER added to the session
                birth = self.recorder.build_birth_event(sources, now)
            snap = self.recorder.build_snapshot(sources, None, now)
            survival = self.recorder.compute_survival(birth, sources, now)

            flags = merged_assessment_flags(sources.assessments)
            provider_backed = any(a.provider_names for a in sources.assessments) or any(
                a.provider not in ("risk-engine", "mock") for a in sources.assessments
            )
            labels = survival["labels"]
            graduated = labels.get("graduated_or_migrated")
            row = FeatureOutcomeRow(
                token_address=token.token_address,
                symbol=token.symbol,
                tape_backed=tape_backed,
                buckets={
                    "top10_concentration": bucket_concentration(
                        flags.get("top10_holder_pct"), cfg.max_top_holder_pct
                    ),
                    "sniper_concentration": bucket_concentration(
                        flags.get("sniper_pct"), cfg.max_sniper_pct
                    ),
                    "insider_concentration": bucket_concentration(
                        flags.get("insider_pct"), cfg.max_insider_pct
                    ),
                    "bundler_concentration": bucket_concentration(
                        flags.get("bundler_pct"), cfg.max_bundler_pct
                    ),
                    "creator_concentration": bucket_concentration(
                        flags.get("creator_pct"), cfg.max_creator_pct
                    ),
                    "risk_level": snap.risk_level or "unknown",
                    "risk_score": bucket_risk_score(snap.risk_score),
                    "liquidity": bucket_liquidity(snap.liquidity_usd),
                    "volume_to_liquidity": bucket_volume_to_liquidity(
                        snap.volume_to_liquidity_24h
                    ),
                    "boost": bucket_boost(snap.boost_amount),
                    "attention": bucket_attention(snap.attention_score),
                    "social_metadata": bucket_social(snap.has_social),
                    "launch_venue": birth.bonding_curve_state or "unknown",
                    "graduation": (
                        "unknown" if graduated is None
                        else ("graduated" if graduated else "not_graduated")
                    ),
                    "provider_coverage": (
                        "provider_backed" if provider_backed else "no_provider_read"
                    ),
                },
                risk_reasons=list(snap.risk_reasons or []),
                missing_info=sorted(
                    set(snap.missing_info or []) | set(birth.missing_info or [])
                ),
                outcomes={name: labels.get(name) for name in ALL_OUTCOMES},
            )
            results.append(row)
        return results, truncated


# --- cohort aggregation + conservative interpretation ----------------------------


def cohort_stats(name: str, group: list[FeatureOutcomeRow], top: int = 3) -> dict:
    """Counts + per-outcome true/false/unknown and a rate over MEASURED
    outcomes only (None never enters a rate)."""
    stats: dict = {
        "cohort": name,
        "n": len(group),
        "label": "measured" if len(group) >= MIN_COHORT_SAMPLES else LABEL_TOO_THIN,
        "outcomes": {},
    }
    for outcome in ALL_OUTCOMES:
        values = [r.outcomes.get(outcome) for r in group]
        true = sum(1 for v in values if v is True)
        false = sum(1 for v in values if v is False)
        unknown = sum(1 for v in values if v is None)
        measured = true + false
        stats["outcomes"][outcome] = {
            "true": true,
            "false": false,
            "unknown": unknown,
            "rate": round(true / measured, 4) if measured >= MIN_MEASURABLE else None,
        }
    stats["examples"] = [
        {
            "token": r.token_address[:16],
            "symbol": r.symbol,
            "outcomes": {k: v for k, v in r.outcomes.items() if v is True},
        }
        for r in group[:top]
    ]
    return stats


def _rate_delta(cohorts: list[dict], outcome: str) -> float | None:
    """Best-vs-worst measured-cohort rate spread for one outcome."""
    rates = [
        c["outcomes"][outcome]["rate"]
        for c in cohorts
        if c["label"] == "measured" and c["outcomes"][outcome]["rate"] is not None
    ]
    if len(rates) < 2:
        return None
    return round(max(rates) - min(rates), 4)


def interpret_dimension(cohorts: list[dict]) -> dict:
    """Conservative interpretation for one feature dimension. Precedence:
    too_thin -> provider_gap_dominates -> strong/weak/none by rate delta."""
    measured = [c for c in cohorts if c["label"] == "measured"]
    if len(measured) < 2:
        return {"label": LABEL_TOO_THIN, "basis": "fewer than 2 measured cohorts"}

    # measurability of the primary survival yardstick across measured cohorts
    total = sum(c["n"] for c in measured)
    known = sum(
        c["outcomes"]["survived_1h"]["true"] + c["outcomes"]["survived_1h"]["false"]
        for c in measured
    )
    if total and known / total < MEASURABILITY_FLOOR:
        return {
            "label": LABEL_GAP_DOMINATED,
            "basis": (
                f"survived_1h measurable for only {known}/{total} tokens "
                "in measured cohorts — collect more tape before reading this"
            ),
        }

    risk_deltas = {
        outcome: _rate_delta(cohorts, outcome) for outcome in RISK_OUTCOMES
    }
    survival_deltas = {
        outcome: _rate_delta(cohorts, outcome) for outcome in SURVIVAL_OUTCOMES
    }
    best_risk = max(
        ((d, o) for o, d in risk_deltas.items() if d is not None), default=(None, None)
    )
    best_survival = max(
        ((d, o) for o, d in survival_deltas.items() if d is not None),
        default=(None, None),
    )
    candidates = []
    if best_risk[0] is not None:
        candidates.append((best_risk[0], LABEL_STRONG_RISK, best_risk[1]))
    if best_survival[0] is not None:
        candidates.append((best_survival[0], LABEL_STRONG_SURVIVAL, best_survival[1]))
    if not candidates:
        return {
            "label": LABEL_GAP_DOMINATED,
            "basis": "no outcome had two measured cohort rates to compare",
        }
    delta, strong_label, outcome = max(candidates)
    if delta >= SEPARATION_STRONG:
        label = strong_label
    elif delta >= SEPARATION_WEAK:
        label = LABEL_WEAK
    else:
        label = LABEL_NO_SEPARATION
    return {
        "label": label,
        "basis": f"max rate delta {delta} on {outcome} across measured cohorts",
        "max_delta": delta,
        "driving_outcome": outcome,
        "risk_deltas": risk_deltas,
        "survival_deltas": survival_deltas,
    }


def _group_by(rows: list[FeatureOutcomeRow], dimension: str) -> dict[str, list]:
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row.buckets[dimension], []).append(row)
    return groups


def _group_multi(rows: list[FeatureOutcomeRow], attr: str) -> dict[str, list]:
    groups: dict[str, list] = {}
    for row in rows:
        for key in getattr(row, attr):
            groups.setdefault(key, []).append(row)
    return groups


DIMENSIONS = (
    "top10_concentration", "sniper_concentration", "insider_concentration",
    "bundler_concentration", "creator_concentration", "risk_level",
    "risk_score", "liquidity", "volume_to_liquidity", "boost", "attention",
    "social_metadata", "launch_venue", "graduation", "provider_coverage",
)


# --- CRYPTO-RETROSPECT-002: tape-backed cohort stratification -------------------


def _filter_by_cohort(rows: list[FeatureOutcomeRow], cohort: str) -> list[FeatureOutcomeRow]:
    if cohort == COHORT_TAPE_BACKED:
        return [r for r in rows if r.tape_backed]
    if cohort == COHORT_DERIVED_ONLY:
        return [r for r in rows if not r.tape_backed]
    return rows


def _has_signal(interp: dict) -> bool:
    return interp.get("label") in SIGNAL_LABELS


def _is_readable(interp: dict) -> bool:
    """Got past too_thin AND the provider-gap floor (produced a rate delta)."""
    return "max_delta" in interp


def _dimension_interpretation(
    rows: list[FeatureOutcomeRow], dimension: str, top: int
) -> dict:
    cohorts = [
        cohort_stats(name, group, top=top)
        for name, group in sorted(_group_by(rows, dimension).items())
    ]
    return interpret_dimension(cohorts)


def _brief(interp: dict) -> dict:
    return {
        "label": interp["label"],
        "max_delta": interp.get("max_delta"),
        "driving_outcome": interp.get("driving_outcome"),
    }


def source_label(
    interp_all: dict, interp_tape: dict, interp_derived: dict,
    n_tape: int, n_derived: int,
) -> str:
    """Where does any apparent signal live? Conservative precedence."""
    tape_readable = _is_readable(interp_tape)
    if not tape_readable:
        # can't read the tape-backed sample at all
        if _is_readable(interp_all) and n_derived > n_tape:
            return SOURCE_DERIVED_DOMINATES  # all-window reads only via derived rows
        return SOURCE_TAPE_TOO_THIN
    # tape-backed is readable
    if _has_signal(interp_tape):
        if _has_signal(interp_derived):
            return (
                SOURCE_CONSISTENT
                if interp_tape.get("driving_outcome") == interp_derived.get("driving_outcome")
                else SOURCE_ALL_DILUTED  # both signal but disagree — combined unreliable
            )
        if _is_readable(interp_derived):
            return SOURCE_ALL_DILUTED     # derived measured-but-flat washes the signal out
        return SOURCE_TAPE_ONLY_HINT      # derived thin/gap: cannot cross-check
    # tape readable but no tape signal
    if _has_signal(interp_derived):
        return SOURCE_DERIVED_DOMINATES
    return SOURCE_TAPE_READABLE


def build_source_stratification(
    all_rows: list[FeatureOutcomeRow], top: int
) -> list[dict]:
    """Per-dimension all/tape-backed/derived-only interpretation + a source
    label + a dilution warning when the all-window view masks a tape signal."""
    tape = [r for r in all_rows if r.tape_backed]
    derived = [r for r in all_rows if not r.tape_backed]
    out: list[dict] = []
    for dimension in DIMENSIONS:
        interp_all = _dimension_interpretation(all_rows, dimension, top)
        interp_tape = _dimension_interpretation(tape, dimension, top)
        interp_derived = _dimension_interpretation(derived, dimension, top)
        label = source_label(interp_all, interp_tape, interp_derived, len(tape), len(derived))
        diluted = _has_signal(interp_tape) and not _has_signal(interp_all)
        warning = None
        if diluted:
            warning = (
                f"all-window hides a tape-backed {interp_tape['label']} on "
                f"{interp_tape.get('driving_outcome')} — read the tape-backed column, "
                "not the all-window one"
            )
        out.append({
            "dimension": dimension,
            "source_label": label,
            "diluted": diluted,
            "warning": warning,
            "all": _brief(interp_all),
            "tape_backed": _brief(interp_tape),
            "derived_only": _brief(interp_derived),
        })
    return out


def _horizon_coverage(rows: list[FeatureOutcomeRow]) -> dict:
    coverage = {}
    for horizon in SURVIVAL_OUTCOMES:
        known = sum(1 for r in rows if r.outcomes.get(horizon) is not None)
        coverage[horizon] = {"known": known, "unknown": len(rows) - known}
    return coverage


def _provider_gap_rate(rows: list[FeatureOutcomeRow]) -> float | None:
    values = [r.outcomes.get("provider_gap") for r in rows]
    measured = [v for v in values if v is not None]
    if not measured:
        return None
    return round(sum(1 for v in measured if v is True) / len(measured), 4)


def build_data_source_mix(all_rows: list[FeatureOutcomeRow]) -> dict:
    tape = [r for r in all_rows if r.tape_backed]
    derived = [r for r in all_rows if not r.tape_backed]
    return {
        "tape_backed": len(tape),
        "derived_only": len(derived),
        # immature = primary yardstick (survived_1h) not yet measurable
        "immature": sum(1 for r in all_rows if r.outcomes.get("survived_1h") is None),
        "horizon_coverage_by_source": {
            "tape_backed": _horizon_coverage(tape),
            "derived_only": _horizon_coverage(derived),
            "all": _horizon_coverage(all_rows),
        },
        "provider_gap_rate_by_source": {
            "tape_backed": _provider_gap_rate(tape),
            "derived_only": _provider_gap_rate(derived),
            "all": _provider_gap_rate(all_rows),
        },
    }


def build_retrospect_report(
    session: Session, hours: int = 48, top: int = 5, cohort: str = COHORT_ALL,
) -> dict:
    """The full retrospective report. Read-only; derived on demand.

    `cohort` re-lenses the HEADLINE (outcome_totals, dimensions, separators)
    to all / tape-backed / derived-only rows. `data_source_mix` and
    `source_stratification` are ALWAYS computed over the full window so the
    tape-vs-derived comparison never disappears."""
    if cohort not in COHORT_CHOICES:
        raise ValueError(f"cohort must be one of {COHORT_CHOICES}, got {cohort!r}")
    service = CryptoRetrospectService()
    all_rows, truncated = service.rows(session, hours=hours)
    rows = _filter_by_cohort(all_rows, cohort)
    now = _now()

    outcome_totals = {}
    for outcome in ALL_OUTCOMES:
        values = [r.outcomes.get(outcome) for r in rows]
        outcome_totals[outcome] = {
            "true": sum(1 for v in values if v is True),
            "false": sum(1 for v in values if v is False),
            "unknown": sum(1 for v in values if v is None),  # immature or gap
        }

    dimensions = []
    for dimension in DIMENSIONS:
        cohorts = [
            cohort_stats(name, group, top=top)
            for name, group in sorted(_group_by(rows, dimension).items())
        ]
        dimensions.append({
            "dimension": dimension,
            "interpretation": interpret_dimension(cohorts),
            "cohorts": cohorts,
        })
    # multi-membership dimensions (a token can appear under several buckets)
    for dimension, attr in (("risk_reason", "risk_reasons"),
                            ("missing_info", "missing_info")):
        groups = sorted(
            _group_multi(rows, attr).items(), key=lambda kv: -len(kv[1])
        )[:max(top * 2, 10)]
        cohorts = [cohort_stats(name, group, top=top) for name, group in groups]
        dimensions.append({
            "dimension": dimension,
            "interpretation": interpret_dimension(cohorts),
            "cohorts": cohorts,
        })

    ranked = sorted(
        (
            d for d in dimensions
            if d["interpretation"].get("max_delta") is not None
        ),
        key=lambda d: -d["interpretation"]["max_delta"],
    )
    best = [
        {
            "dimension": d["dimension"],
            "label": d["interpretation"]["label"],
            "max_delta": d["interpretation"]["max_delta"],
            "driving_outcome": d["interpretation"]["driving_outcome"],
        }
        for d in ranked[:top]
    ]
    worst = [
        {
            "dimension": d["dimension"],
            "label": d["interpretation"]["label"],
            "max_delta": d["interpretation"]["max_delta"],
            "driving_outcome": d["interpretation"]["driving_outcome"],
        }
        for d in ranked[-top:][::-1]
    ] if ranked else []
    unreadable = [
        {"dimension": d["dimension"], "label": d["interpretation"]["label"],
         "basis": d["interpretation"]["basis"]}
        for d in dimensions
        if d["interpretation"]["label"] in (LABEL_TOO_THIN, LABEL_GAP_DOMINATED)
    ]

    # CRYPTO-RETROSPECT-002: source stratification is ALWAYS over the full
    # window (independent of the --cohort headline lens).
    data_source_mix = build_data_source_mix(all_rows)
    source_stratification = build_source_stratification(all_rows, top)
    diluted_dimensions = [s for s in source_stratification if s["diluted"]]

    return {
        "note": RETROSPECT_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "cohort": cohort,
        "tokens_analyzed": len(rows),           # reflects the --cohort lens
        "window_tokens": len(all_rows),         # full window (all sources)
        "tape_backed_tokens": sum(1 for r in all_rows if r.tape_backed),
        "derived_only_tokens": sum(1 for r in all_rows if not r.tape_backed),
        "universe_truncated": truncated,
        "universe_cap": MAX_TOKENS,
        "data_source_mix": data_source_mix,
        "outcome_totals": outcome_totals,
        "dimensions": dimensions,
        "best_separators": best,
        "worst_separators": worst,
        "unreadable_dimensions": unreadable,
        "source_stratification": source_stratification,
        "diluted_dimensions": diluted_dimensions,
        "thresholds": {
            "min_cohort_samples": MIN_COHORT_SAMPLES,
            "min_measurable": MIN_MEASURABLE,
            "separation_weak": SEPARATION_WEAK,
            "separation_strong": SEPARATION_STRONG,
            "measurability_floor": MEASURABILITY_FLOOR,
        },
        "disclaimer": (
            "retrospective measurement only — evidence about which features "
            "separate measured token outcomes, for review triage and future "
            "milestone design; never advice; no EV, no recommendation, no "
            "sizing, no orders, no wallets, no execution"
        ),
    }
