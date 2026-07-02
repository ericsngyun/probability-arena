"""Resolution-criteria assessment: how clear and objective is a market's
settlement rule?

Judges implement `async assess(market) -> ResolutionAssessment`:

- RuleBasedResolutionJudge — deterministic text heuristics; always available,
  never touches the network. The default.
- MockResolutionJudge — canned assessments for tests.
- LLMResolutionJudge — optional (ENABLE_LLM_RESOLUTION=true); refines the
  rule-based baseline with a Claude structured-output call and falls back to
  the baseline on any error. Never required by tests.

Read-only market intelligence: judges observe rules text; nothing here can
place orders.
"""

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import MarketResolutionAssessment
from app.schemas import MarketData, ResolutionAssessment
from app.services.eligibility import market_type_flags

logger = logging.getLogger(__name__)

# Ambiguity flags / rejection reasons (machine-readable, stable strings)
FLAG_MISSING_RULES = "missing_rules_text"
FLAG_RULES_TOO_SHORT = "rules_text_too_short"
FLAG_SUBJECTIVE_WORDING = "subjective_wording"
FLAG_UNCLEAR_SETTLEMENT_SOURCE = "unclear_settlement_source"
FLAG_MULTI_CONDITION = "multi_condition_phrasing"
FLAG_LLM_ERROR_FALLBACK = "llm_error_fallback"
REASON_CLARITY_BELOW_MIN = "clarity_below_min"

# Vague terms penalized when they appear in title/rules text. Placeholder-level
# heuristic: no attempt yet to detect whether the term is precisely defined
# elsewhere in the rules.
SUBJECTIVE_TERMS = (
    "significant",
    "significantly",
    "major",
    "substantial",
    "substantially",
    "expected",
    "likely",
    "unlikely",
    "widely",
    "generally",
    "considered",
    "roughly",
    "serious",
    "notable",
)

SOURCE_PATTERNS = (
    r"according to ([^.;\n]{3,100})",
    r"as (?:published|reported|announced|determined|listed|posted) by ([^.;\n]{3,100})",
    r"\b(official [a-z][a-z0-9 ,'/-]{2,80})",
    r"\b((?:https?://)?(?:[\w-]+\.)+(?:gov|com|org|net|edu)(?:/\S*)?)",
)

MULTI_CONDITION_MARKERS = (
    "all of the following",
    "each of the following",
    "both of the following",
    "in combination",
)

PENALTY_SUBJECTIVE_EACH = 0.15
PENALTY_SUBJECTIVE_CAP = 0.45
PENALTY_NO_SOURCE = 0.25
PENALTY_MULTI_CONDITION = 0.20
PENALTY_RULES_TOO_SHORT = 0.20
MIN_RULES_LENGTH = 40

RISK_LOW_THRESHOLD = 0.80
RISK_MEDIUM_THRESHOLD = 0.55
AVOID_THRESHOLD = 0.40


def detect_settlement_source(rules_text: str) -> str | None:
    for pattern in SOURCE_PATTERNS:
        match = re.search(pattern, rules_text, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _found_subjective_terms(text: str) -> list[str]:
    lowered = text.lower()
    return sorted(
        term for term in SUBJECTIVE_TERMS if re.search(rf"\b{re.escape(term)}\b", lowered)
    )


def _is_multi_condition(market: MarketData, rules_text: str) -> bool:
    lowered = rules_text.lower()
    if any(marker in lowered for marker in MULTI_CONDITION_MARKERS):
        return True
    if lowered.count(" and ") >= 3:
        return True
    return any(market_type_flags(market).values())


def _risk_for(score: float) -> str:
    if score >= RISK_LOW_THRESHOLD:
        return "low"
    if score >= RISK_MEDIUM_THRESHOLD:
        return "medium"
    return "high"


def _tradeability_for(score: float, min_clarity: float) -> str:
    if score >= min_clarity:
        return "researchable"
    if score >= AVOID_THRESHOLD:
        return "needs_manual_review"
    return "avoid"


def _summarize_rules(rules_text: str) -> str:
    first_sentence = rules_text.split(". ")[0].strip()
    return first_sentence[:240]


class RuleBasedResolutionJudge:
    """Deterministic heuristics over the market's rules text. Same input
    always yields the same assessment."""

    model_name = "rule-based"

    def __init__(self, min_clarity_score: float | None = None, prompt_version: str | None = None):
        settings = get_settings()
        self.min_clarity_score = (
            min_clarity_score if min_clarity_score is not None else settings.min_clarity_score
        )
        self.prompt_version = prompt_version or settings.resolution_prompt_version

    async def assess(self, market: MarketData) -> ResolutionAssessment:
        rules_text = (market.rules_primary or "").strip()
        if not rules_text:
            return ResolutionAssessment(
                clarity_score=0.0,
                resolution_risk="unknown",
                tradeability="needs_manual_review",
                settlement_source=None,
                resolution_summary="No resolution rules text available.",
                ambiguity_flags=[FLAG_MISSING_RULES],
                rejection_reasons=[FLAG_MISSING_RULES, REASON_CLARITY_BELOW_MIN],
                llm_confidence=None,
            )

        flags: list[str] = []
        reasons: list[str] = []
        score = 1.0

        subjective = _found_subjective_terms(f"{market.title} {rules_text}")
        if subjective:
            score -= min(PENALTY_SUBJECTIVE_CAP, PENALTY_SUBJECTIVE_EACH * len(subjective))
            flags.extend(f"{FLAG_SUBJECTIVE_WORDING}:{term}" for term in subjective)

        # A known source from detail enrichment beats rules-text detection
        settlement_source = market.settlement_source or detect_settlement_source(rules_text)
        if settlement_source is None:
            score -= PENALTY_NO_SOURCE
            flags.append(FLAG_UNCLEAR_SETTLEMENT_SOURCE)

        if _is_multi_condition(market, rules_text):
            score -= PENALTY_MULTI_CONDITION
            flags.append(FLAG_MULTI_CONDITION)

        if len(rules_text) < MIN_RULES_LENGTH:
            score -= PENALTY_RULES_TOO_SHORT
            flags.append(FLAG_RULES_TOO_SHORT)

        score = round(max(score, 0.0), 4)
        if score < self.min_clarity_score:
            reasons.append(REASON_CLARITY_BELOW_MIN)

        return ResolutionAssessment(
            clarity_score=score,
            resolution_risk=_risk_for(score),
            tradeability=_tradeability_for(score, self.min_clarity_score),
            settlement_source=settlement_source,
            resolution_summary=_summarize_rules(rules_text),
            ambiguity_flags=flags,
            rejection_reasons=reasons,
            llm_confidence=None,
        )


class MockResolutionJudge:
    """Canned assessments for tests; records the tickers it was asked about."""

    model_name = "mock"
    prompt_version = "v1"

    def __init__(self, assessment: ResolutionAssessment | None = None):
        self.assessment = assessment or ResolutionAssessment(
            clarity_score=0.9,
            resolution_risk="low",
            tradeability="researchable",
            settlement_source="Mock Settlement Bureau",
            resolution_summary="Mock assessment.",
            ambiguity_flags=[],
            rejection_reasons=[],
            llm_confidence=0.9,
        )
        self.assessed_tickers: list[str] = []

    async def assess(self, market: MarketData) -> ResolutionAssessment:
        self.assessed_tickers.append(market.ticker)
        return self.assessment


RESOLUTION_SYSTEM_PROMPT_V1 = """You are a prediction-market resolution auditor.
Given a market's title and settlement rules, judge how clear and objective the
resolution criteria are. Penalize subjective wording, missing or ambiguous
settlement sources, and multi-condition/parlay phrasing. You are advising a
read-only research pipeline; never suggest trades."""

RESOLUTION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "clarity_score": {"type": "number"},
        "resolution_risk": {"type": "string", "enum": ["low", "medium", "high", "unknown"]},
        "tradeability": {
            "type": "string",
            "enum": ["researchable", "avoid", "needs_manual_review"],
        },
        "settlement_source": {"type": ["string", "null"]},
        "resolution_summary": {"type": "string"},
        "ambiguity_flags": {"type": "array", "items": {"type": "string"}},
        "rejection_reasons": {"type": "array", "items": {"type": "string"}},
        "llm_confidence": {"type": "number"},
    },
    "required": [
        "clarity_score",
        "resolution_risk",
        "tradeability",
        "settlement_source",
        "resolution_summary",
        "ambiguity_flags",
        "rejection_reasons",
        "llm_confidence",
    ],
    "additionalProperties": False,
}


class LLMResolutionJudge:
    """Optional Claude-backed judge. Computes the rule-based baseline first and
    falls back to it (flagged) on any import/credential/API failure, so the
    pipeline never hard-fails because of the LLM."""

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.model_name = settings.resolution_model_name
        self.prompt_version = settings.resolution_prompt_version
        self.min_clarity_score = settings.min_clarity_score
        self._baseline = RuleBasedResolutionJudge(
            min_clarity_score=settings.min_clarity_score,
            prompt_version=settings.resolution_prompt_version,
        )

    def _render_market(self, market: MarketData, baseline: ResolutionAssessment) -> str:
        return json.dumps(
            {
                "ticker": market.ticker,
                "title": market.title,
                "category": market.category,
                "close_time": market.close_time.isoformat() if market.close_time else None,
                "rules_primary": market.rules_primary,
                "rule_based_baseline": baseline.model_dump(exclude={"raw_response"}),
                "min_clarity_score": self.min_clarity_score,
            },
            default=str,
        )

    async def assess(self, market: MarketData) -> ResolutionAssessment:
        baseline = await self._baseline.assess(market)
        try:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=self.model_name,
                max_tokens=1024,
                system=RESOLUTION_SYSTEM_PROMPT_V1,
                output_config={
                    "format": {"type": "json_schema", "schema": RESOLUTION_OUTPUT_SCHEMA}
                },
                messages=[{"role": "user", "content": self._render_market(market, baseline)}],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("LLM refused the assessment request")
            text = next(block.text for block in response.content if block.type == "text")
            data = json.loads(text)
            assessment = ResolutionAssessment(**data)
            assessment.raw_response = data
            # Enforce the configured clarity gate regardless of the LLM verdict
            if (
                assessment.clarity_score < self.min_clarity_score
                and REASON_CLARITY_BELOW_MIN not in assessment.rejection_reasons
            ):
                assessment.rejection_reasons.append(REASON_CLARITY_BELOW_MIN)
                assessment.tradeability = _tradeability_for(
                    assessment.clarity_score, self.min_clarity_score
                )
            return assessment
        except Exception:
            logger.exception(
                "LLM resolution assessment failed for %s; using rule-based fallback",
                market.ticker,
            )
            baseline.ambiguity_flags = [*baseline.ambiguity_flags, FLAG_LLM_ERROR_FALLBACK]
            return baseline


def get_judge(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.enable_llm_resolution:
        return LLMResolutionJudge(settings)
    return RuleBasedResolutionJudge(
        min_clarity_score=settings.min_clarity_score,
        prompt_version=settings.resolution_prompt_version,
    )


def persist_assessment(
    session: Session,
    market_ticker: str,
    assessment: ResolutionAssessment,
    judge,
    scanner_run_id: int | None = None,
) -> MarketResolutionAssessment:
    row = MarketResolutionAssessment(
        market_ticker=market_ticker,
        scanner_run_id=scanner_run_id,
        model_name=judge.model_name,
        prompt_version=getattr(judge, "prompt_version", "v1"),
        clarity_score=assessment.clarity_score,
        resolution_risk=assessment.resolution_risk,
        tradeability=assessment.tradeability,
        settlement_source=assessment.settlement_source,
        resolution_summary=assessment.resolution_summary,
        ambiguity_flags=assessment.ambiguity_flags,
        rejection_reasons=assessment.rejection_reasons,
        llm_confidence=assessment.llm_confidence,
        raw_response=assessment.raw_response
        or assessment.model_dump(exclude={"raw_response"}),
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row
