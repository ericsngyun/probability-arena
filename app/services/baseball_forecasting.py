"""Baseball evidence-aware forecaster (MVP-004F).

Consumes persisted source-backed MLB research packets (from the MVP-004E
canary) and produces capped, explainable, NON-midpoint probability forecasts
for recognized market types. It never calls external APIs itself — evidence
comes exclusively from the packet's key_facts / raw_response.

Eligibility (checked by ForecastingService, re-checked here):
domain sports_baseball + packet evidence_depth source_backed + completeness
>= BASEBALL_FORECAST_MIN_COMPLETENESS + resolution researchable. Anything
else — including unrecognized market types — falls back to the template
baseline with a skeptic note.

Model (deterministic v1, deliberately naive and fully stated in the output):
market midpoint is the prior; live game state produces an evidence estimate
(pace-projected totals, current margin for spreads/winner); the blend weight
and slope grow with game progress (late-game evidence moves the needle more);
the total shift away from the prior is hard-capped at MAX_PRIOR_SHIFT.

Read-only, no EV, no trade semantics."""

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
from app.services.baseball_research import MLB_SOURCE_NAME, parse_mlb_ticker
from app.services.forecasting import (
    EVIDENCE_SOURCE_BACKED,
    ForecastInput,
    TemplateBaselineForecaster,
    confidence_cap_for,
    determine_evidence_depth,
    is_critical_info_missing,
)

logger = logging.getLogger(__name__)

FORECASTER_NAME = "baseball_evidence"
TAG_V1 = "baseball_evidence_v1"

MARKET_TYPE_TOTAL = "total"
MARKET_TYPE_SPREAD = "spread"
MARKET_TYPE_WINNER = "winner"
MARKET_TYPE_UNKNOWN = "unknown"

LEAGUE_AVG_TOTAL_RUNS = 8.6  # MLB full-game average, v1 pace model constant
MAX_PRIOR_SHIFT = 0.25  # hard cap on |p - prior|
PROB_FLOOR, PROB_CEIL = 0.02, 0.98
LATE_GAME_INNING = 7

GAME_STATE_RE = re.compile(r"(Top|Bottom|Middle|End)\s+(\d+),\s*(\d+)\s+out")
BASES_RE = re.compile(r"bases:\s*([a-z/]+|empty)")
SCORE_FACT_RE = re.compile(r"Game state: .*?(\d+)\s*—\s*.*?(\d+)")


@dataclass
class BaseballEvidence:
    away_runs: int | None = None
    home_runs: int | None = None
    inning: int | None = None
    inning_half: str | None = None
    outs: int | None = None
    runners_on: int = 0
    abstract_state: str | None = None  # Preview|Live|Final
    lineups_confirmed: bool = False
    probable_pitchers: bool = False
    weather_known: bool = False

    @property
    def is_live(self) -> bool:
        return self.abstract_state == "Live" and self.inning is not None

    @property
    def is_final(self) -> bool:
        return self.abstract_state == "Final"

    @property
    def current_total(self) -> int | None:
        if self.away_runs is None or self.home_runs is None:
            return None
        return self.away_runs + self.home_runs

    @property
    def progress(self) -> float:
        """Fraction of a 9-inning game completed (0..1)."""
        if self.is_final:
            return 1.0
        if self.inning is None:
            return 0.0
        half_done = 0.5 if self.inning_half in ("Bottom", "End") else 0.0
        return min(1.0, max(0.0, ((self.inning - 1) + half_done) / 9))


def extract_baseball_evidence(packet) -> BaseballEvidence:
    """Structured evidence from a persisted packet: raw_response['extracted']
    first, game-state fact text for inning/outs/bases."""
    evidence = BaseballEvidence()
    raw = packet.raw_response or {}
    extracted = raw.get("extracted") or {}
    score = extracted.get("score") or {}
    if score.get("away") is not None and score.get("home") is not None:
        evidence.away_runs = int(score["away"])
        evidence.home_runs = int(score["home"])
    evidence.abstract_state = extracted.get("abstract_state")
    evidence.lineups_confirmed = bool(extracted.get("lineups_confirmed"))
    evidence.probable_pitchers = bool(extracted.get("probable_pitchers"))
    evidence.weather_known = bool(extracted.get("weather"))

    for fact in packet.key_facts or []:
        if (fact.get("source_name") or "") != MLB_SOURCE_NAME:
            continue
        text = fact.get("fact") or ""
        if text.startswith("Game state:"):
            state_match = GAME_STATE_RE.search(text)
            if state_match:
                evidence.inning_half = state_match.group(1)
                evidence.inning = int(state_match.group(2))
                evidence.outs = int(state_match.group(3))
            bases_match = BASES_RE.search(text)
            if bases_match and bases_match.group(1) != "empty":
                evidence.runners_on = len(bases_match.group(1).split("/"))
            if evidence.away_runs is None:
                score_match = SCORE_FACT_RE.search(text)
                if score_match:
                    evidence.away_runs = int(score_match.group(1))
                    evidence.home_runs = int(score_match.group(2))
        elif text.startswith("Probable pitchers"):
            evidence.probable_pitchers = True
        elif text.startswith("Confirmed lineups"):
            evidence.lineups_confirmed = True
        elif text.startswith("Weather"):
            evidence.weather_known = True
    return evidence


@dataclass
class MarketSpec:
    market_type: str
    threshold: float | None = None  # total line, or spread margin (x.5)
    team: str | None = None  # spread/winner subject team abbreviation
    team_is_home: bool | None = None


def parse_market_spec(ticker: str) -> MarketSpec:
    """Conservative market-type detection from the Kalshi ticker. Only
    KXMLBTOTAL / KXMLBSPREAD / KXMLBGAME are recognized; player props, F5,
    and anything else map to 'unknown' (template fallback)."""
    series = ticker.split("-")[0].upper()
    suffix = ticker.rsplit("-", 1)[-1].upper()
    context = parse_mlb_ticker(ticker)
    matchup = context.matchup if context else ""

    def team_side(team: str) -> bool | None:
        if matchup.startswith(team) and matchup.endswith(team):
            return None  # ambiguous
        if matchup.startswith(team):
            return False  # away
        if matchup.endswith(team):
            return True  # home
        return None

    if series == "KXMLBTOTAL" and suffix.isdigit():
        # assumed semantics: resolves YES if final total >= suffix
        return MarketSpec(market_type=MARKET_TYPE_TOTAL, threshold=float(suffix) - 0.5)
    if series == "KXMLBSPREAD":
        match = re.match(r"^([A-Z]{2,3})(\d+)$", suffix)
        if match:
            team, digits = match.groups()
            side = team_side(team)
            if side is not None:
                # assumed semantics: team wins by more than digits - 0.5
                return MarketSpec(
                    market_type=MARKET_TYPE_SPREAD,
                    threshold=float(digits) - 0.5,
                    team=team,
                    team_is_home=side,
                )
    if series == "KXMLBGAME" and suffix.isalpha():
        side = team_side(suffix)
        if side is not None:
            return MarketSpec(market_type=MARKET_TYPE_WINNER, team=suffix, team_is_home=side)
    return MarketSpec(market_type=MARKET_TYPE_UNKNOWN)


def _clamp_probability(value: float) -> float:
    return round(min(max(value, PROB_FLOOR), PROB_CEIL), 4)


class BaseballEvidenceAwareForecaster:
    """Deterministic evidence-aware forecaster for recognized MLB market
    types. Falls back to TemplateBaselineForecaster (with a skeptic note)
    whenever eligibility or market-type recognition fails."""

    model_name: str | None = None

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = FORECASTER_NAME
        self.version = settings.baseball_forecaster_version
        self.prompt_version = settings.baseball_forecaster_version
        self.max_confidence = settings.baseball_forecast_max_confidence
        self.min_completeness = settings.baseball_forecast_min_completeness
        self.settings = settings
        self._template = TemplateBaselineForecaster(settings)

    async def _fallback(self, inp: ForecastInput, reason: str, extra_tag: str | None = None) -> MarketForecast:
        forecast = await self._template.forecast(inp)
        forecast.skeptic_notes = [
            *forecast.skeptic_notes,
            f"Evidence-aware baseball forecasting not applied: {reason}",
        ]
        if extra_tag:
            forecast.calibration_tags = [*forecast.calibration_tags, extra_tag]
        return forecast

    def _eligible(self, inp: ForecastInput) -> str | None:
        """None when eligible, else the reason for falling back."""
        if inp.domain != "sports_baseball":
            return f"domain is {inp.domain}, not sports_baseball"
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

        spec = parse_market_spec(inp.market.ticker)
        if spec.market_type == MARKET_TYPE_UNKNOWN:
            return await self._fallback(
                inp,
                "market type not recognized (only totals/spreads/game-winner are supported in v1)",
                extra_tag="market_type_unknown",
            )

        evidence = extract_baseball_evidence(inp.packet)
        market = inp.market
        two_sided = market.yes_bid is not None and market.yes_ask is not None
        prior = (
            round((market.yes_bid + market.yes_ask) / 2 / 100, 4) if two_sided else 0.5
        )

        estimate, estimate_notes = self._evidence_estimate(spec, evidence)
        progress = evidence.progress
        phase_tag = "late_game" if (evidence.inning or 0) >= LATE_GAME_INNING or evidence.is_final else "early_game"

        adjusted = False
        if estimate is not None:
            weight = 0.30 + 0.45 * progress  # late-game evidence weighs more
            shift = max(-MAX_PRIOR_SHIFT, min(MAX_PRIOR_SHIFT, weight * (estimate - prior)))
            probability = _clamp_probability(prior + shift)
            adjusted = abs(shift) > 1e-9
        else:
            probability = _clamp_probability(prior)

        confidence = 0.35
        if evidence.is_live or evidence.is_final:
            confidence += 0.10
        if evidence.lineups_confirmed:
            confidence += 0.05
        if evidence.probable_pitchers:
            confidence += 0.05
        if phase_tag == "late_game":
            confidence += 0.05
        if (inp.packet.research_completeness_score or 0.0) >= 0.9:
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
        elif phase_tag == "late_game" and confidence >= 0.6:
            risk = "low"

        direction = None
        if adjusted:
            direction = "above" if probability > prior else "below"
        state_text = (
            f"{evidence.away_runs}-{evidence.home_runs}, "
            f"{evidence.inning_half} {evidence.inning}, {evidence.outs} out(s)"
            if evidence.is_live
            else (evidence.abstract_state or "pregame")
        )

        bull_points = [f"Official game state ({MLB_SOURCE_NAME}): {state_text}"]
        bear_points = []
        for note in estimate_notes:
            bull_points.append(note) if direction == "above" else bear_points.append(note)
        if adjusted:
            bull_points.append(
                f"Evidence estimate {estimate:.2f} vs market prior {prior:.2f} "
                f"(blend weight {0.30 + 0.45 * progress:.2f}, shift capped at ±{MAX_PRIOR_SHIFT})"
            )
        else:
            bear_points.append("No evidence-based adjustment applied; probability stays at prior")
        if not bear_points:
            bear_points.append("v1 pace/margin model ignores pitching changes and leverage")

        skeptic_notes = [
            "Deterministic v1 model: naive pace/margin projection, no simulations",
            f"Adjustment away from the market prior is hard-capped at ±{MAX_PRIOR_SHIFT}",
        ]
        if spec.threshold is not None:
            skeptic_notes.append(
                f"Market line assumed from ticker suffix: {spec.threshold} "
                "(if Kalshi semantics differ, the estimate direction may be wrong)"
            )

        assumptions = [
            ForecastAssumption(
                assumption="Ticker suffix encodes the market line/subject as assumed",
                criticality="high",
            ),
            ForecastAssumption(
                assumption="MLB Stats API game state was accurate at packet creation",
                criticality="medium",
            ),
            ForecastAssumption(
                assumption=f"Remaining scoring follows league-average pace "
                f"({LEAGUE_AVG_TOTAL_RUNS} runs/9 innings)",
                criticality="high",
            ),
        ]
        change_triggers = [
            ForecastChangeTrigger(trigger="Score changes since packet creation", direction="unclear"),
            ForecastChangeTrigger(
                trigger="Game progressing further (late innings sharpen the estimate)",
                direction="unclear",
            ),
            ForecastChangeTrigger(trigger="Late scratches or pitching changes", direction="unclear"),
        ]

        tags = [
            inp.domain,
            EVIDENCE_SOURCE_BACKED,
            TAG_V1,
            f"market_type_{spec.market_type}",
            phase_tag,
        ]
        if evidence.is_live:
            tags.append("live_game_state")
        tags.append("evidence_adjusted" if adjusted else "anchored_to_market_mid")

        summary = (
            f"Evidence-aware probability {probability:.2f} for '{market.title or market.ticker}' "
            f"({spec.market_type} market, {phase_tag.replace('_', ' ')}). Prior {prior:.2f}"
            + (f", evidence estimate {estimate:.2f}." if estimate is not None else ", no adjustment.")
            + " Reasoning artifact only."
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
                    "threshold": spec.threshold,
                    "team": spec.team,
                    "team_is_home": spec.team_is_home,
                },
                "evidence": {
                    "away_runs": evidence.away_runs,
                    "home_runs": evidence.home_runs,
                    "inning": evidence.inning,
                    "inning_half": evidence.inning_half,
                    "outs": evidence.outs,
                    "abstract_state": evidence.abstract_state,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _evidence_estimate(
        self, spec: MarketSpec, evidence: BaseballEvidence
    ) -> tuple[float | None, list[str]]:
        """(evidence probability estimate, explanation notes) or (None, notes)
        when the evidence can't support an estimate."""
        notes: list[str] = []
        if not (evidence.is_live or evidence.is_final):
            return None, ["Game has not started (or state unavailable); no live evidence to apply"]
        progress = evidence.progress

        if spec.market_type == MARKET_TYPE_TOTAL:
            current = evidence.current_total
            if current is None or spec.threshold is None:
                return None, ["Score or market line unavailable"]
            if current > spec.threshold:
                return 0.97, [f"Current total {current} already exceeds the {spec.threshold} line"]
            projected = current + LEAGUE_AVG_TOTAL_RUNS * (1 - progress)
            slope = 0.08 + 0.12 * progress
            estimate = _clamp_probability(0.5 + (projected - spec.threshold) * slope)
            notes.append(
                f"Pace projection: {current} runs through {progress:.0%} of the game "
                f"-> ~{projected:.1f} final vs line {spec.threshold}"
            )
            return estimate, notes

        if spec.market_type in (MARKET_TYPE_SPREAD, MARKET_TYPE_WINNER):
            if evidence.away_runs is None or evidence.home_runs is None:
                return None, ["Score unavailable"]
            team_runs = evidence.home_runs if spec.team_is_home else evidence.away_runs
            opp_runs = evidence.away_runs if spec.team_is_home else evidence.home_runs
            margin = team_runs - opp_runs
            needed = spec.threshold if spec.market_type == MARKET_TYPE_SPREAD else 0.0
            if evidence.is_final:
                return (0.97 if margin > needed else 0.03), [
                    f"Game final: {spec.team} margin {margin:+d} vs required >{needed}"
                ]
            slope = 0.06 + 0.10 * progress
            estimate = _clamp_probability(0.5 + (margin - needed) * slope)
            notes.append(
                f"{spec.team} margin {margin:+d} through {progress:.0%} of the game "
                f"vs required >{needed}"
            )
            return estimate, notes

        return None, ["Unsupported market type"]
