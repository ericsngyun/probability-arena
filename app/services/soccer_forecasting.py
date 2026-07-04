"""Soccer evidence-aware forecaster (SOCCER-002).

Consumes persisted source-backed soccer/World Cup research packets (from the
SOCCER-001 canary) and produces capped, explainable, NON-midpoint probability
forecasts for recognized market types. It never calls external APIs itself —
evidence comes exclusively from the packet's key_facts / raw_response.

Eligibility (checked by ForecastingService, re-checked here): domain
sports_soccer + packet evidence_depth source_backed + completeness >=
SOCCER_FORECAST_MIN_COMPLETENESS + resolution researchable. Anything else —
including unrecognized market types and player-goal markets without
player-specific evidence — falls back to the template baseline with a
skeptic note.

Model (deterministic v1, deliberately naive and fully stated in the output):
market midpoint is the prior; live match state produces an evidence estimate
(goal margin for winner/advance, pace-projected goals for totals); the blend
weight grows with match progress (late match and extra time move the needle
more); penalty shootouts are high-uncertainty except for team-to-advance;
red cards add context and reduce confidence without inflating the estimate;
the total shift away from the prior is hard-capped at MAX_PRIOR_SHIFT.

Read-only. Forecasts are measurement inputs only — no dollar EV, no trade
recommendations, no sizing, no orders, no execution of any kind."""

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
from app.services.soccer_research import parse_soccer_ticker

logger = logging.getLogger(__name__)

FORECASTER_NAME = "soccer_evidence"
TAG_V1 = "soccer_evidence_v1"

MARKET_TYPE_WINNER = "winner"
MARKET_TYPE_ADVANCE = "advance"
MARKET_TYPE_TOTAL = "total"
MARKET_TYPE_SPREAD = "spread"
MARKET_TYPE_PLAYER_GOAL = "player_goal"
MARKET_TYPE_UNKNOWN = "unknown"

LEAGUE_AVG_TOTAL_GOALS = 2.7  # full-match average, v1 pace model constant
MAX_PRIOR_SHIFT = 0.25  # hard cap on |p - prior|
PROB_FLOOR, PROB_CEIL = 0.02, 0.98
LATE_MATCH_MINUTE = 60
REGULATION_MINUTES = 90

CLOCK_RE = re.compile(r"clock\s+(\d+)")
MATCH_STATE_SCORE_RE = re.compile(r"Match state: .*?(\d+)\s*—\s*.*?(\d+)")
STAT_PAIR_RE = re.compile(r"(possessionPct|totalShots|shotsOnTarget)\s+([\d.]+)–([\d.]+)")


def _int_or_none(value) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


@dataclass
class SoccerEvidence:
    home_goals: int | None = None
    away_goals: int | None = None
    minute: int | None = None
    period: int | None = None
    state: str | None = None  # pre|in|post
    status_text: str | None = None
    shootout: bool = False
    shootout_home: int | None = None
    shootout_away: int | None = None
    red_cards: int = 0
    lineups_confirmed: bool = False
    stats_available: bool = False
    possession_home: float | None = None
    shots_home: float | None = None
    shots_away: float | None = None

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    @property
    def is_final(self) -> bool:
        return self.state == "post"

    @property
    def extra_time(self) -> bool:
        return bool(
            (self.period is not None and self.period > 2)
            or (self.minute is not None and self.minute > REGULATION_MINUTES)
        )

    @property
    def current_total(self) -> int | None:
        if self.home_goals is None or self.away_goals is None:
            return None
        return self.home_goals + self.away_goals

    @property
    def progress(self) -> float:
        """Fraction of regulation completed (0..1); final counts as 1."""
        if self.is_final:
            return 1.0
        if self.minute is None:
            return 0.0
        return min(1.0, max(0.0, self.minute / REGULATION_MINUTES))


def extract_soccer_evidence(packet) -> SoccerEvidence:
    """Structured evidence from a persisted packet: raw_response['extracted']
    first, fact text as backup (mirrors the baseball extractor)."""
    evidence = SoccerEvidence()
    raw = packet.raw_response or {}
    extracted = raw.get("extracted") or {}

    score = extracted.get("score") or {}
    evidence.home_goals = _int_or_none(score.get("home"))
    evidence.away_goals = _int_or_none(score.get("away"))
    evidence.state = extracted.get("state")
    evidence.status_text = extracted.get("status")
    clock = extracted.get("clock")
    if clock:
        digits = re.search(r"(\d+)", str(clock))
        if digits:
            evidence.minute = int(digits.group(1))
    evidence.period = _int_or_none(extracted.get("period"))
    evidence.lineups_confirmed = bool(extracted.get("lineups_confirmed"))
    red_cards = extracted.get("red_cards")
    if isinstance(red_cards, int):
        evidence.red_cards = red_cards
    shootout = extracted.get("shootout")
    if shootout:
        evidence.shootout = True
        if isinstance(shootout, dict):
            evidence.shootout_home = _int_or_none(shootout.get("home"))
            evidence.shootout_away = _int_or_none(shootout.get("away"))
    if extracted.get("stats"):
        evidence.stats_available = True

    for fact in packet.key_facts or []:
        text = fact.get("fact") or ""
        if text.startswith("Match state:"):
            if evidence.home_goals is None:
                score_match = MATCH_STATE_SCORE_RE.search(text)
                if score_match:
                    evidence.home_goals = int(score_match.group(1))
                    evidence.away_goals = int(score_match.group(2))
            if evidence.minute is None:
                clock_match = CLOCK_RE.search(text)
                if clock_match:
                    evidence.minute = int(clock_match.group(1))
        elif text.startswith("Confirmed lineups"):
            evidence.lineups_confirmed = True
        elif text.startswith("Red cards:") and "none" not in text:
            evidence.red_cards = max(evidence.red_cards, text.count("("))
        elif text.startswith("Penalty shootout"):
            evidence.shootout = True
        elif text.startswith("Match stats"):
            evidence.stats_available = True
            for name, home_value, away_value in STAT_PAIR_RE.findall(text):
                if name == "possessionPct":
                    evidence.possession_home = float(home_value)
                elif name == "totalShots":
                    evidence.shots_home = float(home_value)
                    evidence.shots_away = float(away_value)
    return evidence


@dataclass
class SoccerMarketSpec:
    market_type: str
    threshold: float | None = None  # total line or spread margin (x.5)
    team: str | None = None
    team_is_home: bool | None = None


def parse_soccer_market_spec(ticker: str) -> SoccerMarketSpec:
    """Conservative market-type detection from the Kalshi ticker. Recognized:
    *TOTAL (over/under), *SPREAD/HANDICAP (team+goals suffix), *GAME/MATCH/
    WIN (winner, team suffix), *ADVANCE (team to advance, team suffix).
    Player-goal series (*GOAL* with player-code suffixes) map to player_goal
    (conservative fallback); everything else is unknown."""
    context = parse_soccer_ticker(ticker)
    if context is None:
        return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    series = ticker.split("-")[0].upper()
    suffix = ticker.rsplit("-", 1)[-1].upper()

    def team_side(team: str) -> bool | None:
        # SOCCER-001 collector records ESPN home first in facts, but the
        # ticker matchup order is (team_a, team_b); side is resolved against
        # the extracted score by name order, so here we only need identity.
        if context.team_a == team:
            return False  # first-listed
        if context.team_b == team:
            return True  # second-listed
        return None

    if series.endswith("TOTAL"):
        line = context.line
        if line is None and suffix.replace(".", "").isdigit():
            line = float(suffix)
        if line is not None:
            # assumed semantics: resolves YES if total goals >= line
            threshold = line - 0.5 if float(line).is_integer() else line
            return SoccerMarketSpec(market_type=MARKET_TYPE_TOTAL, threshold=threshold)
        return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    if "ADVANCE" in series:
        side = team_side(suffix) if suffix.isalpha() else None
        if side is not None:
            return SoccerMarketSpec(
                market_type=MARKET_TYPE_ADVANCE, team=suffix, team_is_home=side
            )
        return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    if "SPREAD" in series or "HANDICAP" in series:
        match = re.match(r"^([A-Z]{2,4})(\d+)$", suffix)
        if match:
            team, digits = match.groups()
            side = team_side(team)
            if side is not None:
                return SoccerMarketSpec(
                    market_type=MARKET_TYPE_SPREAD,
                    threshold=float(digits) - 0.5,
                    team=team,
                    team_is_home=side,
                )
        return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    if "GOAL" in series:
        # player-goal / anytime-scorer style markets: team-level match data
        # cannot price a specific player — conservative by design
        return SoccerMarketSpec(market_type=MARKET_TYPE_PLAYER_GOAL)
    if any(marker in series for marker in ("GAME", "MATCH", "WIN")):
        side = team_side(suffix) if suffix.isalpha() else None
        if side is not None:
            return SoccerMarketSpec(
                market_type=MARKET_TYPE_WINNER, team=suffix, team_is_home=side
            )
        return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)
    return SoccerMarketSpec(market_type=MARKET_TYPE_UNKNOWN)


def _clamp_probability(value: float) -> float:
    return round(min(max(value, PROB_FLOOR), PROB_CEIL), 4)


class SoccerEvidenceAwareForecaster:
    """Deterministic evidence-aware forecaster for recognized soccer market
    types. Falls back to TemplateBaselineForecaster (with a skeptic note)
    whenever eligibility or market-type recognition fails."""

    model_name: str | None = None

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = FORECASTER_NAME
        self.version = settings.soccer_forecaster_version
        self.prompt_version = settings.soccer_forecaster_version
        self.max_confidence = settings.soccer_forecast_max_confidence
        self.min_completeness = settings.soccer_forecast_min_completeness
        self.settings = settings
        self._template = TemplateBaselineForecaster(settings)

    async def _fallback(
        self, inp: ForecastInput, reason: str, extra_tag: str | None = None
    ) -> MarketForecast:
        forecast = await self._template.forecast(inp)
        forecast.skeptic_notes = [
            *forecast.skeptic_notes,
            f"Evidence-aware soccer forecasting not applied: {reason}",
        ]
        if extra_tag:
            forecast.calibration_tags = [*forecast.calibration_tags, extra_tag]
        return forecast

    def _eligible(self, inp: ForecastInput) -> str | None:
        if inp.domain != "sports_soccer":
            return f"domain is {inp.domain}, not sports_soccer"
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

        spec = parse_soccer_market_spec(inp.market.ticker)
        if spec.market_type == MARKET_TYPE_UNKNOWN:
            return await self._fallback(
                inp,
                "market type not recognized (winner/advance/total/spread supported in v1)",
                extra_tag="market_type_unknown",
            )
        if spec.market_type == MARKET_TYPE_PLAYER_GOAL:
            return await self._fallback(
                inp,
                "player-goal market: team-level match evidence cannot price a "
                "specific player (no player-specific evidence in v1)",
                extra_tag="market_type_player_goal",
            )

        evidence = extract_soccer_evidence(inp.packet)
        market = inp.market
        two_sided = market.yes_bid is not None and market.yes_ask is not None
        prior = (
            round((market.yes_bid + market.yes_ask) / 2 / 100, 4) if two_sided else 0.5
        )

        estimate, estimate_notes = self._evidence_estimate(spec, evidence)
        progress = evidence.progress
        late = (
            evidence.is_final
            or evidence.extra_time
            or (evidence.minute or 0) >= LATE_MATCH_MINUTE
        )
        phase_tag = "late_match" if late else "early_match"

        adjusted = False
        if estimate is not None:
            weight = 0.30 + 0.45 * progress  # late-match evidence weighs more
            if evidence.extra_time:
                weight = min(0.80, weight + 0.05)
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
        if evidence.stats_available:
            confidence += 0.05
        if late:
            confidence += 0.05
        if (inp.packet.research_completeness_score or 0.0) >= 0.9:
            confidence += 0.05
        if evidence.red_cards > 0:
            confidence -= 0.05  # discipline chaos: conservative, never a boost
        if evidence.shootout and spec.market_type != MARKET_TYPE_ADVANCE:
            confidence = min(confidence, 0.50)  # shootouts are coin-flip territory
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
            f"{evidence.home_goals}-{evidence.away_goals}, minute {evidence.minute}, "
            f"period {evidence.period}"
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
                f"(shift capped at ±{MAX_PRIOR_SHIFT})"
            )
        else:
            bear_points.append("No evidence-based adjustment applied; probability stays at prior")
        if not bear_points:
            bear_points.append(
                "v1 margin/pace model ignores momentum, substitutions, and tactics"
            )

        skeptic_notes = [
            "Deterministic v1 model: naive goal-margin/pace projection, no simulations",
            f"Adjustment away from the market prior is hard-capped at ±{MAX_PRIOR_SHIFT}",
        ]
        if spec.threshold is not None:
            skeptic_notes.append(
                f"Market line assumed from ticker: {spec.threshold} "
                "(if Kalshi semantics differ, the estimate direction may be wrong)"
            )
        if evidence.red_cards > 0:
            skeptic_notes.append(
                f"Red card(s) in this match ({evidence.red_cards}): game state is less "
                "stable than the score suggests; confidence reduced, estimate NOT boosted"
            )
        if evidence.shootout:
            skeptic_notes.append(
                "Penalty shootout context: outcomes are close to coin flips; "
                "only team-to-advance markets use shootout scores"
            )

        assumptions = [
            ForecastAssumption(
                assumption="Ticker encodes the market subject/line as assumed",
                criticality="high",
            ),
            ForecastAssumption(
                assumption="ESPN match state was accurate at packet creation",
                criticality="medium",
            ),
            ForecastAssumption(
                assumption=f"Remaining scoring follows league-average pace "
                f"({LEAGUE_AVG_TOTAL_GOALS} goals/match)",
                criticality="high",
            ),
        ]
        change_triggers = [
            ForecastChangeTrigger(trigger="Goals since packet creation", direction="unclear"),
            ForecastChangeTrigger(
                trigger="Match progressing further (late match sharpens the estimate)",
                direction="unclear",
            ),
            ForecastChangeTrigger(trigger="Red cards or key substitutions", direction="unclear"),
        ]

        tags = [
            inp.domain,
            EVIDENCE_SOURCE_BACKED,
            TAG_V1,
            f"market_type_{spec.market_type}",
            phase_tag,
        ]
        if evidence.is_live:
            tags.append("live_match_state")
        if evidence.extra_time:
            tags.append("extra_time")
        if evidence.shootout:
            tags.append("penalty_context")
        if evidence.red_cards > 0:
            tags.append("red_card_context")
        tags.append("evidence_adjusted" if adjusted else "anchored_to_market_mid")

        summary = (
            f"Evidence-aware probability {probability:.2f} for "
            f"'{market.title or market.ticker}' ({spec.market_type} market, "
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
                    "threshold": spec.threshold,
                    "team": spec.team,
                    "team_is_home": spec.team_is_home,
                },
                "evidence": {
                    "home_goals": evidence.home_goals,
                    "away_goals": evidence.away_goals,
                    "minute": evidence.minute,
                    "period": evidence.period,
                    "state": evidence.state,
                    "extra_time": evidence.extra_time,
                    "shootout": evidence.shootout,
                    "red_cards": evidence.red_cards,
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _evidence_estimate(
        self, spec: SoccerMarketSpec, evidence: SoccerEvidence
    ) -> tuple[float | None, list[str]]:
        """(evidence probability estimate, explanation notes) or (None, notes)
        when the evidence can't support one."""
        notes: list[str] = []
        if not (evidence.is_live or evidence.is_final):
            return None, ["Match has not started (or state unavailable); no live evidence"]
        progress = evidence.progress

        if spec.market_type == MARKET_TYPE_TOTAL:
            current = evidence.current_total
            if current is None or spec.threshold is None:
                return None, ["Score or market line unavailable"]
            if current > spec.threshold:
                return 0.97, [
                    f"Current total {current} already exceeds the {spec.threshold} line"
                ]
            if evidence.is_final and not evidence.shootout:
                return 0.03, [
                    f"Match final at {current} goals, below the {spec.threshold} line"
                ]
            projected = current + LEAGUE_AVG_TOTAL_GOALS * (1 - progress)
            slope = 0.10 + 0.15 * progress
            estimate = _clamp_probability(0.5 + (projected - spec.threshold) * slope)
            notes.append(
                f"Pace projection: {current} goals through {progress:.0%} of regulation "
                f"-> ~{projected:.1f} final vs line {spec.threshold}"
            )
            return estimate, notes

        if spec.market_type in (MARKET_TYPE_WINNER, MARKET_TYPE_ADVANCE, MARKET_TYPE_SPREAD):
            if evidence.home_goals is None or evidence.away_goals is None:
                return None, ["Score unavailable"]
            team_goals = evidence.home_goals if spec.team_is_home else evidence.away_goals
            opp_goals = evidence.away_goals if spec.team_is_home else evidence.home_goals
            margin = team_goals - opp_goals
            needed = spec.threshold if spec.market_type == MARKET_TYPE_SPREAD else 0.0

            if evidence.shootout:
                if spec.market_type != MARKET_TYPE_ADVANCE:
                    return None, [
                        "Penalty shootout: win/spread outcomes are effectively coin "
                        "flips in v1 (no estimate applied)"
                    ]
                if evidence.shootout_home is not None and evidence.shootout_away is not None:
                    pens_margin = (
                        (evidence.shootout_home - evidence.shootout_away)
                        if spec.team_is_home
                        else (evidence.shootout_away - evidence.shootout_home)
                    )
                    if evidence.is_final:
                        return (0.97 if pens_margin > 0 else 0.03), [
                            f"Shootout final: {spec.team} margin {pens_margin:+d}"
                        ]
                    estimate = _clamp_probability(0.5 + pens_margin * 0.15)
                    return estimate, [
                        f"Shootout in progress: {spec.team} margin {pens_margin:+d}"
                    ]
                return None, ["Shootout under way but scores unavailable"]

            if evidence.is_final:
                if spec.market_type == MARKET_TYPE_WINNER:
                    return (0.97 if margin > 0 else 0.03), [
                        f"Match final: {spec.team} margin {margin:+d} (draw resolves NO)"
                    ]
                return (0.97 if margin > needed else 0.03), [
                    f"Match final: {spec.team} margin {margin:+d} vs required >{needed}"
                ]

            if margin == 0 and spec.market_type == MARKET_TYPE_WINNER:
                # level match: a draw grows likelier as time drains — the
                # win probability decays below the coin flip
                estimate = _clamp_probability(0.5 - 0.20 * progress)
                notes.append(
                    f"Level at {evidence.minute}': draw increasingly likely for a "
                    "must-WIN market"
                )
                return estimate, notes
            if margin == 0 and spec.market_type == MARKET_TYPE_ADVANCE:
                notes.append(
                    "Level match: extra time/penalties keep advancement near the prior"
                )
                return 0.5, notes

            slope = 0.15 + 0.20 * progress  # a goal in soccer is decisive late
            estimate = _clamp_probability(0.5 + (margin - needed) * slope)
            notes.append(
                f"{spec.team} margin {margin:+d} through {progress:.0%} of regulation "
                + (f"vs required >{needed}" if spec.market_type == MARKET_TYPE_SPREAD else "")
            )
            return estimate, notes

        return None, ["Unsupported market type"]
