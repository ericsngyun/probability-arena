"""Tennis evidence-aware forecaster (TENNIS-001).

Consumes persisted source-backed tennis research packets (from the TENNIS-001
canary) and produces capped, explainable, NON-midpoint probability forecasts
for MATCH-WINNER markets only (v1). It never calls external APIs itself —
evidence comes exclusively from the packet's key_facts / raw_response.

Eligibility (checked by ForecastingService, re-checked here): domain
sports_tennis + packet evidence_depth source_backed + completeness >=
TENNIS_FORECAST_MIN_COMPLETENESS + resolution researchable + a recognized
match-winner market. Anything else — including set/game/total/prop markets and
unparseable tickers — falls back to the template baseline with a skeptic note.

Model (deterministic v1, deliberately naive and fully stated in the output):
market midpoint is the prior; live set/game margin for the subject player
produces an evidence estimate; the blend weight grows with match progress
(a set lead late in a best-of-3 is close to decisive); retirement/walkover and
completed matches resolve near-certain; missing critical facts cap confidence
at 0.50 and high risk; the total shift away from the prior is hard-capped
TIGHTLY at MAX_PRIOR_SHIFT.

Read-only. Forecasts are measurement inputs only — no dollar EV, no trade
recommendations, no sizing, no orders, no wallets/keys, no swaps, no signing,
no execution of any kind."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.schemas import (
    ForecastAssumption,
    ForecastCase,
    ForecastChangeTrigger,
    MarketForecast,
)
from app.services.forecasting import (
    EVIDENCE_SOURCE_BACKED,
    ForecastInput,
    TemplateBaselineForecaster,
    confidence_cap_for,
    determine_evidence_depth,
    is_critical_info_missing,
)
from app.services.tennis_research import parse_tennis_ticker

logger = logging.getLogger(__name__)

FORECASTER_NAME = "tennis_evidence"
TAG_V1 = "tennis_evidence_v1"

MARKET_TYPE_WINNER = "winner"
MARKET_TYPE_UNKNOWN = "unknown"

DEFAULT_BEST_OF = 3  # conservative v1 assumption (most tour matches); stated as an assumption
MAX_PRIOR_SHIFT = 0.20  # hard cap on |p - prior| (tighter than soccer's 0.25)
PROB_FLOOR, PROB_CEIL = 0.02, 0.98

SET_STATE_RE = re.compile(r"sets\s+(\d+)-(\d+)")


def _int_or_none(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class TennisEvidence:
    sets_a: int | None = None  # sets WON by player A
    sets_b: int | None = None
    games_a: int | None = None  # games in the current set
    games_b: int | None = None
    state: str | None = None  # pre|in|post
    status_text: str | None = None
    winner: str | None = None  # "a"|"b"|None
    retirement: bool = False
    server: int | None = None
    surface: str | None = None
    ranks_available: bool = False

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def is_final(self) -> bool:
        return self.state == "post" or self.winner is not None

    @property
    def sets_needed(self) -> int:
        return DEFAULT_BEST_OF // 2 + 1  # 2 for best-of-3

    @property
    def progress(self) -> float:
        """Fraction of the match completed (0..1); final counts as 1."""
        if self.is_final:
            return 1.0
        if self.sets_a is None or self.sets_b is None:
            return 0.0
        completed = self.sets_a + self.sets_b
        return min(1.0, completed / DEFAULT_BEST_OF)


def extract_tennis_evidence(packet) -> TennisEvidence:
    """Structured evidence from a persisted packet: raw_response['extracted']
    first, fact text as backup."""
    evidence = TennisEvidence()
    raw = packet.raw_response or {}
    extracted = raw.get("extracted") or {}

    sets = extracted.get("sets") or {}
    evidence.sets_a = _int_or_none(sets.get("a"))
    evidence.sets_b = _int_or_none(sets.get("b"))
    games = extracted.get("games") or {}
    evidence.games_a = _int_or_none(games.get("a"))
    evidence.games_b = _int_or_none(games.get("b"))
    evidence.state = extracted.get("state")
    evidence.status_text = extracted.get("status")
    evidence.winner = extracted.get("winner")
    evidence.retirement = bool(extracted.get("retirement"))
    evidence.server = _int_or_none(extracted.get("server"))
    evidence.surface = extracted.get("surface")
    evidence.ranks_available = bool(extracted.get("ranks"))

    for fact in packet.key_facts or []:
        text = fact.get("fact") or ""
        if text.startswith("Match state:") and evidence.sets_a is None:
            m = SET_STATE_RE.search(text)
            if m:
                evidence.sets_a = int(m.group(1))
                evidence.sets_b = int(m.group(2))
        elif text.startswith("Result:") and "retire" in text.lower():
            evidence.retirement = True
    return evidence


@dataclass
class TennisMarketSpec:
    market_type: str
    player: str | None = None
    player_is_a: bool | None = None


def parse_tennis_market_spec(ticker: str) -> TennisMarketSpec:
    """Conservative market-type detection. v1 recognizes match-winner series
    only (MATCH/WINNER/WIN/GAME) where the ticker suffix identifies the subject
    player; everything else is unknown (honest fallback)."""
    context = parse_tennis_ticker(ticker)
    if context is None or context.market_type != MARKET_TYPE_WINNER:
        return TennisMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    suffix = ticker.rsplit("-", 1)[-1].upper()

    def player_side(code: str) -> bool | None:
        if context.player_a == code:
            return True  # subject is player A (first-listed)
        if context.player_b == code:
            return False
        # player codes need not split evenly; match the subject against the
        # start (player A) or end (player B) of the concatenated matchup
        starts = context.matchup.startswith(code)
        ends = context.matchup.endswith(code)
        if starts and not ends:
            return True
        if ends and not starts:
            return False
        return None

    if suffix.isalpha():
        side = player_side(suffix)
        if side is not None:
            return TennisMarketSpec(
                market_type=MARKET_TYPE_WINNER, player=suffix, player_is_a=side
            )
    return TennisMarketSpec(market_type=MARKET_TYPE_UNKNOWN)


def _clamp_probability(value: float) -> float:
    return round(min(max(value, PROB_FLOOR), PROB_CEIL), 4)


class TennisEvidenceAwareForecaster:
    """Deterministic evidence-aware forecaster for match-winner tennis markets.
    Falls back to TemplateBaselineForecaster (with a skeptic note) whenever
    eligibility or market-type recognition fails."""

    model_name: str | None = None

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = FORECASTER_NAME
        self.version = settings.tennis_forecaster_version
        self.prompt_version = settings.tennis_forecaster_version
        self.max_confidence = settings.tennis_forecast_max_confidence
        self.min_completeness = settings.tennis_forecast_min_completeness
        self.settings = settings
        self._template = TemplateBaselineForecaster(settings)

    async def _fallback(
        self, inp: ForecastInput, reason: str, extra_tag: str | None = None
    ) -> MarketForecast:
        forecast = await self._template.forecast(inp)
        forecast.skeptic_notes = [
            *forecast.skeptic_notes,
            f"Evidence-aware tennis forecasting not applied: {reason}",
        ]
        if extra_tag:
            forecast.calibration_tags = [*forecast.calibration_tags, extra_tag]
        return forecast

    def _eligible(self, inp: ForecastInput) -> str | None:
        if inp.domain != "sports_tennis":
            return f"domain is {inp.domain}, not sports_tennis"
        if determine_evidence_depth(inp.packet) != EVIDENCE_SOURCE_BACKED:
            return "research packet is not source_backed"
        if (inp.packet.research_completeness_score or 0.0) < self.min_completeness:
            return (
                f"completeness {inp.packet.research_completeness_score} below "
                f"{self.min_completeness}"
            )
        if inp.resolution is None or inp.resolution.tradeability != "researchable":
            return "resolution assessment is missing or not researchable"
        return None

    async def forecast(self, inp: ForecastInput) -> MarketForecast:
        reason = self._eligible(inp)
        if reason is not None:
            return await self._fallback(inp, reason)

        spec = parse_tennis_market_spec(inp.market.ticker)
        if spec.market_type != MARKET_TYPE_WINNER:
            return await self._fallback(
                inp,
                "market type not recognized (match-winner only in v1)",
                extra_tag="market_type_unknown",
            )

        evidence = extract_tennis_evidence(inp.packet)
        market = inp.market
        two_sided = market.yes_bid is not None and market.yes_ask is not None
        prior = round((market.yes_bid + market.yes_ask) / 2 / 100, 4) if two_sided else 0.5

        estimate, estimate_notes = self._evidence_estimate(spec, evidence)
        progress = evidence.progress
        late = evidence.is_final or progress >= 0.5
        phase_tag = "late_match" if late else "early_match"

        adjusted = False
        if estimate is not None:
            weight = 0.30 + 0.40 * progress  # a set lead weighs more late
            shift = max(-MAX_PRIOR_SHIFT, min(MAX_PRIOR_SHIFT, weight * (estimate - prior)))
            probability = _clamp_probability(prior + shift)
            adjusted = abs(shift) > 1e-9
        else:
            probability = _clamp_probability(prior)

        confidence = 0.35
        if evidence.is_live or evidence.is_final:
            confidence += 0.10
        if evidence.sets_a is not None:
            confidence += 0.05
        if evidence.ranks_available:
            confidence += 0.05
        if late:
            confidence += 0.05
        if estimate is None:
            confidence = min(confidence, 0.50)  # missing critical facts cap
        critical_missing = is_critical_info_missing(inp.packet, inp.resolution)
        confidence = round(
            min(
                confidence,
                self.max_confidence,
                confidence_cap_for(EVIDENCE_SOURCE_BACKED, critical_missing, self.settings),
            ),
            4,
        )

        risk = "medium"
        if critical_missing or estimate is None:
            risk = "high"
        elif late and confidence >= 0.6:
            risk = "low"

        direction = "above" if (adjusted and probability > prior) else "below"
        state_text = (
            f"sets {evidence.sets_a}-{evidence.sets_b}, games {evidence.games_a}-{evidence.games_b}"
            if evidence.is_live
            else (evidence.status_text or evidence.state or "pre-match")
        )

        bull_points = [f"Live match state (packet evidence): {state_text}"]
        bear_points = []
        for note in estimate_notes:
            (bull_points if direction == "above" else bear_points).append(note)
        if adjusted:
            bull_points.append(
                f"Evidence estimate {estimate:.2f} vs market prior {prior:.2f} "
                f"(shift capped tightly at ±{MAX_PRIOR_SHIFT})"
            )
        else:
            bear_points.append("No evidence-based adjustment applied; probability stays at prior")
        if not bear_points:
            bear_points.append("v1 set-margin model ignores serve strength, surface form, and momentum")

        skeptic_notes = [
            "Deterministic v1 model: naive set-margin projection, no point-by-point simulation",
            f"Adjustment away from the market prior is hard-capped at ±{MAX_PRIOR_SHIFT}",
            f"Best-of-{DEFAULT_BEST_OF} assumed (Grand Slam men's matches are best-of-5; if so the "
            "estimate under-weights comebacks)",
        ]
        if evidence.retirement:
            skeptic_notes.append("Retirement/walkover context detected: outcome is effectively settled")

        assumptions = [
            ForecastAssumption(
                assumption="Ticker suffix identifies the market's subject player",
                criticality="high",
            ),
            ForecastAssumption(
                assumption=f"Match is best-of-{DEFAULT_BEST_OF}", criticality="high"
            ),
            ForecastAssumption(
                assumption="ESPN match state was accurate at packet creation",
                criticality="medium",
            ),
        ]
        change_triggers = [
            ForecastChangeTrigger(trigger="Sets/games since packet creation", direction="unclear"),
            ForecastChangeTrigger(
                trigger="Match progressing further (late sharpens the estimate)", direction="unclear"
            ),
            ForecastChangeTrigger(trigger="Retirement or medical timeout", direction="unclear"),
        ]

        tags = [
            inp.domain,
            EVIDENCE_SOURCE_BACKED,
            TAG_V1,
            "market_type_winner",
            phase_tag,
        ]
        if evidence.is_live or evidence.is_final:
            tags.append("match_state")
        if evidence.retirement:
            tags.append("retirement_context")
        tags.append("evidence_adjusted" if adjusted else "evidence_insufficient")

        summary = (
            f"Evidence-aware probability {probability:.2f} for "
            f"'{market.title or market.ticker}' (match-winner market, "
            f"{phase_tag.replace('_', ' ')}). Prior {prior:.2f}"
            + (f", evidence estimate {estimate:.2f}." if estimate is not None else ", no adjustment.")
            + " Measurement input only."
        )

        return MarketForecast(
            estimated_probability=probability,
            confidence=confidence,
            evidence_depth=EVIDENCE_SOURCE_BACKED,
            forecast_risk=risk,
            forecast_summary=summary,
            bull_case=ForecastCase(thesis="Case for YES resolution", points=bull_points),
            bear_case=ForecastCase(thesis="Case for NO resolution", points=bear_points),
            skeptic_notes=skeptic_notes,
            key_assumptions=assumptions,
            missing_info=list(inp.packet.missing_info or []),
            what_would_change_mind=change_triggers,
            calibration_tags=tags,
            raw_response={
                "model": TAG_V1,
                "prior": prior,
                "evidence_estimate": estimate,
                "progress": round(progress, 4),
                "market_spec": {
                    "market_type": spec.market_type,
                    "player": spec.player,
                    "player_is_a": spec.player_is_a,
                },
                "evidence": {
                    "sets_a": evidence.sets_a,
                    "sets_b": evidence.sets_b,
                    "games_a": evidence.games_a,
                    "games_b": evidence.games_b,
                    "state": evidence.state,
                    "winner": evidence.winner,
                    "retirement": evidence.retirement,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _evidence_estimate(
        self, spec: TennisMarketSpec, evidence: TennisEvidence
    ) -> tuple[float | None, list[str]]:
        """(evidence probability estimate for the subject player winning,
        explanation notes) or (None, notes) when evidence can't support one."""
        notes: list[str] = []
        if not (evidence.is_live or evidence.is_final):
            return None, ["Match has not started (or state unavailable); no live evidence"]
        if evidence.sets_a is None or evidence.sets_b is None:
            return None, ["Set score unavailable"]

        subj_sets = evidence.sets_a if spec.player_is_a else evidence.sets_b
        opp_sets = evidence.sets_b if spec.player_is_a else evidence.sets_a
        subj_games = (evidence.games_a if spec.player_is_a else evidence.games_b) or 0
        opp_games = (evidence.games_b if spec.player_is_a else evidence.games_a) or 0

        # Settled: winner known or retirement
        if evidence.is_final:
            if evidence.winner is not None:
                subj_won = (evidence.winner == "a") == bool(spec.player_is_a)
                return (0.97 if subj_won else 0.03), [
                    f"Match final ({evidence.status_text or 'completed'}): subject "
                    + ("won" if subj_won else "lost")
                ]
            # final by set count
            return (0.97 if subj_sets > opp_sets else 0.03), [
                f"Match final: sets {subj_sets}-{opp_sets}"
            ]

        # Already reached sets needed (match effectively won)
        if subj_sets >= evidence.sets_needed:
            return 0.95, [f"Subject has {subj_sets} sets (needs {evidence.sets_needed})"]
        if opp_sets >= evidence.sets_needed:
            return 0.05, [f"Opponent has {opp_sets} sets (needs {evidence.sets_needed})"]

        progress = evidence.progress
        set_margin = subj_sets - opp_sets
        game_margin = subj_games - opp_games
        set_slope = 0.18 + 0.10 * progress  # a set lead is decisive late in a bo3
        estimate = _clamp_probability(0.5 + set_margin * set_slope + game_margin * 0.02)
        notes.append(
            f"Set margin {set_margin:+d} (games {subj_games}-{opp_games} in current set) "
            f"through {progress:.0%} of a best-of-{DEFAULT_BEST_OF}"
        )
        return estimate, notes
