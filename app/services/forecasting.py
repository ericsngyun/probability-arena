"""Structured forecast engine: turns persisted research packets into
probability forecasts with explicit evidence depth and capped confidence.

Forecasters implement `async forecast(inp: ForecastInput) -> MarketForecast`:

- TemplateBaselineForecaster — deterministic neutral prior from packet and
  market structure; never touches the network. The default.
- MockForecaster — canned forecasts for tests.
- LLMForecaster — optional (ENABLE_LLM_FORECASTING=true); consumes enrichment,
  resolution assessment, and research packet via a Claude structured-output
  call and falls back to the template baseline on any failure.

Deterministic post-processing in ForecastingService recomputes evidence_depth
and applies the confidence caps to EVERY forecast regardless of which
forecaster produced it.

Hard boundary for this layer: forecasts are probabilities and reasoning
artifacts only — no EV, no sizing, no execution, no directives of any kind.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    Market,
    MarketForecastRecord,
    MarketResearchPacket,
    MarketResolutionAssessment,
    MarketSnapshot,
)
from app.schemas import (
    ForecastAssumption,
    ForecastCase,
    ForecastChangeTrigger,
    MarketData,
    MarketForecast,
)
from app.services.enrichment import apply_enrichment, latest_enrichment_for
from app.services.research import latest_packet_for, market_data_from_row
from app.services.resolution import latest_assessment_for

logger = logging.getLogger(__name__)

EVIDENCE_TEMPLATE_ONLY = "template_only"
EVIDENCE_SOURCE_BACKED = "source_backed"
EVIDENCE_MIXED = "mixed"

# Fact provenance names the TemplateResearchCollector emits from local
# metadata; anything else counts as external evidence.
TEMPLATE_FACT_SOURCE_NAMES = frozenset(
    {"kalshi_series_metadata", "kalshi_market_metadata", "kalshi_rules_text"}
)
TEMPLATE_COMPLETENESS_CEILING = 0.65

MISSING_SETTLEMENT_MARKER = "settlement source unresolved"
FALLBACK_NOTE = "LLM forecasting unavailable; deterministic template baseline used instead"


class MissingResearchPacketError(RuntimeError):
    """Raised when a forecast is requested for a market with no packet."""


def _apply_latest_snapshot(
    session: Session, market: Market, market_data: MarketData
) -> MarketData:
    """Overlay the latest persisted quote/activity snapshot — the markets
    table holds metadata only; quotes live on market_snapshots."""
    from sqlalchemy import select

    snapshot = session.execute(
        select(MarketSnapshot)
        .where(MarketSnapshot.market_id == market.id)
        .order_by(MarketSnapshot.captured_at.desc(), MarketSnapshot.id.desc())
    ).scalars().first()
    if snapshot is None:
        return market_data
    return market_data.model_copy(
        update={
            "yes_bid": snapshot.yes_bid,
            "yes_ask": snapshot.yes_ask,
            "volume_24h": snapshot.volume_24h,
            "open_interest": snapshot.open_interest,
            "liquidity": snapshot.liquidity,
        }
    )


@dataclass
class ForecastInput:
    market: MarketData  # enrichment already applied
    packet: MarketResearchPacket
    resolution: MarketResolutionAssessment | None

    @property
    def domain(self) -> str:
        return self.packet.domain


def determine_evidence_depth(packet: MarketResearchPacket) -> str:
    """Deterministic evidence-depth classification for a packet.

    template_only: at/below the template completeness ceiling with no facts
    beyond local Kalshi metadata. source_backed: external facts AND above the
    ceiling. mixed: everything in between."""
    score = packet.research_completeness_score or 0.0
    facts = packet.key_facts or []
    external_facts = [
        fact for fact in facts if (fact.get("source_name") or "") not in TEMPLATE_FACT_SOURCE_NAMES
    ]
    if score <= TEMPLATE_COMPLETENESS_CEILING and not external_facts:
        return EVIDENCE_TEMPLATE_ONLY
    if score > TEMPLATE_COMPLETENESS_CEILING and external_facts:
        return EVIDENCE_SOURCE_BACKED
    return EVIDENCE_MIXED


def is_critical_info_missing(
    packet: MarketResearchPacket, resolution: MarketResolutionAssessment | None
) -> bool:
    """Critical gaps: unresolved settlement source, no resolution assessment,
    or a resolution verdict other than researchable."""
    if MISSING_SETTLEMENT_MARKER in (packet.missing_info or []):
        return True
    if resolution is None:
        return True
    return resolution.tradeability != "researchable"


def confidence_cap_for(
    evidence_depth: str, critical_missing: bool, settings: Settings | None = None
) -> float:
    settings = settings or get_settings()
    cap = (
        settings.template_only_max_confidence
        if evidence_depth == EVIDENCE_TEMPLATE_ONLY
        else settings.source_backed_max_confidence
    )
    if critical_missing:
        cap = min(cap, settings.missing_critical_info_max_confidence)
    return cap


def _forecast_risk(
    evidence_depth: str, confidence: float, critical_missing: bool, structurally_simple: bool
) -> str:
    if critical_missing:
        return "high"
    if evidence_depth == EVIDENCE_TEMPLATE_ONLY:
        return "medium" if structurally_simple else "high"
    return "low" if confidence >= 0.6 else "medium"


def _is_structurally_simple(market: MarketData, resolution) -> bool:
    """Binary market with a live two-sided quote, a known settlement source,
    and a researchable resolution verdict."""
    two_sided = market.yes_bid is not None and market.yes_ask is not None
    researchable = resolution is not None and resolution.tradeability == "researchable"
    return two_sided and researchable and bool(market.settlement_source)


class TemplateBaselineForecaster:
    """Deterministic neutral prior. Anchors to the market midpoint when a
    two-sided quote exists (public consensus as prior), otherwise 0.50.
    Adds no independent information — and says so in its own skeptic notes."""

    model_name: str | None = None

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = settings.forecaster_name
        self.version = settings.forecaster_version
        self.prompt_version = settings.forecast_prompt_version
        self.settings = settings

    async def forecast(self, inp: ForecastInput) -> MarketForecast:
        market = inp.market
        packet = inp.packet
        resolution = inp.resolution
        missing_info = list(packet.missing_info or [])

        two_sided = market.yes_bid is not None and market.yes_ask is not None
        if two_sided:
            probability = round((market.yes_bid + market.yes_ask) / 2 / 100, 4)
            probability = min(max(probability, 0.02), 0.98)
            prior_tag = "anchored_to_market_mid"
        else:
            probability = 0.5
            prior_tag = "uninformative_prior"

        researchable = resolution is not None and resolution.tradeability == "researchable"
        confidence = 0.30
        if researchable:
            confidence += 0.10
        if market.settlement_source:
            confidence += 0.05
        if two_sided:
            confidence += 0.05

        evidence_depth = determine_evidence_depth(packet)
        critical_missing = is_critical_info_missing(packet, resolution)
        confidence = round(
            min(confidence, confidence_cap_for(evidence_depth, critical_missing, self.settings)),
            4,
        )
        structurally_simple = _is_structurally_simple(market, resolution)
        risk = _forecast_risk(evidence_depth, confidence, critical_missing, structurally_simple)

        bull_points: list[str] = []
        bear_points: list[str] = []
        if researchable:
            bull_points.append(
                f"Resolution criteria are clear (clarity {resolution.clarity_score:.2f}, researchable)"
            )
        if market.settlement_source:
            bull_points.append(f"Settlement source is known: {market.settlement_source}")
        if two_sided:
            bull_points.append(
                f"Two-sided quote present (yes {market.yes_bid}c / {market.yes_ask}c), "
                "so public consensus is observable"
            )
        if not bull_points:
            bull_points.append("No structural strengths identified from local metadata")

        if missing_info:
            preview = "; ".join(missing_info[:2])
            bear_points.append(f"{len(missing_info)} research gaps remain (e.g. {preview})")
        if evidence_depth == EVIDENCE_TEMPLATE_ONLY:
            bear_points.append("No external evidence has been gathered yet (template-only packet)")
        if not researchable:
            bear_points.append("Resolution assessment is missing or not researchable")
        if not bear_points:
            bear_points.append("Residual event uncertainty until the outcome is observed")

        skeptic_notes = [
            "Template baseline adds no information beyond public quotes and Kalshi metadata",
            f"Confidence is capped at {confidence_cap_for(evidence_depth, critical_missing, self.settings):.2f} "
            f"for {evidence_depth} evidence",
        ]
        if prior_tag == "anchored_to_market_mid":
            skeptic_notes.append(
                "Probability is anchored to the quoted midpoint; independent research could move it materially"
            )

        assumptions = [
            ForecastAssumption(
                assumption="The stated settlement source will report the outcome accurately",
                criticality="high",
            ),
            ForecastAssumption(
                assumption="Market metadata (rules, close time) is current as of packet creation",
                criticality="medium",
            ),
        ]
        if prior_tag == "anchored_to_market_mid":
            assumptions.append(
                ForecastAssumption(
                    assumption="Quoted prices reflect currently available public information",
                    criticality="medium",
                )
            )

        change_triggers = [
            ForecastChangeTrigger(trigger=f"New evidence on: {gap}", direction="unclear")
            for gap in missing_info[:3]
        ]
        if two_sided:
            change_triggers.append(
                ForecastChangeTrigger(
                    trigger="Material move in the quoted midpoint before close",
                    direction="unclear",
                )
            )

        summary = (
            f"Neutral baseline probability {probability:.2f} for '{market.title or market.ticker}' "
            f"({inp.domain}). Evidence depth {evidence_depth}; confidence {confidence:.2f} "
            f"(capped). Reasoning artifact only."
        )

        return MarketForecast(
            estimated_probability=probability,
            confidence=confidence,
            evidence_depth=evidence_depth,
            forecast_risk=risk,
            forecast_summary=summary,
            bull_case=ForecastCase(thesis="Case for YES resolution", points=bull_points),
            bear_case=ForecastCase(thesis="Case for NO resolution", points=bear_points),
            skeptic_notes=skeptic_notes,
            key_assumptions=assumptions,
            missing_info=missing_info,
            what_would_change_mind=change_triggers,
            calibration_tags=[inp.domain, evidence_depth, prior_tag, "template_baseline_v1"],
        )


class MockForecaster:
    """Canned forecasts for tests; records the tickers it was asked about."""

    name = "mock"
    version = "v1"
    prompt_version = "v1"
    model_name: str | None = None

    def __init__(self, forecast: MarketForecast | None = None):
        self.canned = forecast or MarketForecast(
            estimated_probability=0.5,
            confidence=0.4,
            evidence_depth=EVIDENCE_MIXED,
            forecast_risk="medium",
            forecast_summary="Mock forecast.",
            bull_case=ForecastCase(thesis="mock bull", points=["mock point"]),
            bear_case=ForecastCase(thesis="mock bear", points=["mock point"]),
            skeptic_notes=["mock skeptic note"],
            key_assumptions=[ForecastAssumption(assumption="mock assumption")],
            missing_info=["mock gap"],
            what_would_change_mind=[ForecastChangeTrigger(trigger="mock trigger")],
        )
        self.forecasted_tickers: list[str] = []

    async def forecast(self, inp: ForecastInput) -> MarketForecast:
        self.forecasted_tickers.append(inp.market.ticker)
        return self.canned


FORECAST_SYSTEM_PROMPT_V1 = """You are a careful probabilistic forecaster for
prediction-market outcomes. Given a market's metadata, resolution assessment,
and research packet, estimate the probability the market resolves YES, with a
bull case, bear case, skeptic notes, key assumptions, and what would change
your mind. Be honest about evidence depth and uncertainty. Output structured
reasoning only — never advice about transactions of any kind."""

FORECAST_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "estimated_probability": {"type": "number"},
        "confidence": {"type": "number"},
        "forecast_summary": {"type": "string"},
        "bull_case": {
            "type": "object",
            "properties": {
                "thesis": {"type": "string"},
                "points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thesis", "points"],
            "additionalProperties": False,
        },
        "bear_case": {
            "type": "object",
            "properties": {
                "thesis": {"type": "string"},
                "points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["thesis", "points"],
            "additionalProperties": False,
        },
        "skeptic_notes": {"type": "array", "items": {"type": "string"}},
        "key_assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "assumption": {"type": "string"},
                    "criticality": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["assumption", "criticality"],
                "additionalProperties": False,
            },
        },
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "what_would_change_mind": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "trigger": {"type": "string"},
                    "direction": {
                        "type": "string",
                        "enum": ["increases_probability", "decreases_probability", "unclear"],
                    },
                },
                "required": ["trigger", "direction"],
                "additionalProperties": False,
            },
        },
        "calibration_tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "estimated_probability",
        "confidence",
        "forecast_summary",
        "bull_case",
        "bear_case",
        "skeptic_notes",
        "key_assumptions",
        "missing_info",
        "what_would_change_mind",
        "calibration_tags",
    ],
    "additionalProperties": False,
}


class LLMForecaster:
    """Optional Claude-backed forecaster. Falls back to the template baseline
    (flagged) on missing packages/credentials, refusals, malformed output, or
    API errors. Evidence depth, confidence caps, and risk are recomputed
    deterministically regardless of what the model reports."""

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = "llm"
        self.version = settings.forecaster_version
        self.prompt_version = settings.forecast_prompt_version
        self.model_name = settings.forecast_model_name
        self.settings = settings
        self._baseline = TemplateBaselineForecaster(settings)

    def _render_input(self, inp: ForecastInput) -> str:
        market = inp.market
        resolution = inp.resolution
        return json.dumps(
            {
                "market": {
                    "ticker": market.ticker,
                    "title": market.title,
                    "rules": market.rules_primary,
                    "settlement_source": market.settlement_source,
                    "close_time": market.close_time.isoformat() if market.close_time else None,
                    "yes_bid_cents": market.yes_bid,
                    "yes_ask_cents": market.yes_ask,
                },
                "domain": inp.domain,
                "resolution_assessment": {
                    "clarity_score": resolution.clarity_score,
                    "tradeability": resolution.tradeability,
                    "resolution_risk": resolution.resolution_risk,
                }
                if resolution
                else None,
                "research_packet": {
                    "completeness_score": inp.packet.research_completeness_score,
                    "key_facts": inp.packet.key_facts,
                    "sources": inp.packet.sources,
                    "missing_info": inp.packet.missing_info,
                },
            },
            default=str,
        )

    async def forecast(self, inp: ForecastInput) -> MarketForecast:
        try:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=self.model_name,
                max_tokens=4096,
                system=FORECAST_SYSTEM_PROMPT_V1,
                output_config={
                    "format": {"type": "json_schema", "schema": FORECAST_OUTPUT_SCHEMA}
                },
                messages=[{"role": "user", "content": self._render_input(inp)}],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("LLM refused the forecast request")
            text = next(block.text for block in response.content if block.type == "text")
            data = json.loads(text)

            evidence_depth = determine_evidence_depth(inp.packet)
            critical_missing = is_critical_info_missing(inp.packet, inp.resolution)
            probability = min(max(float(data["estimated_probability"]), 0.0), 1.0)
            confidence = round(
                min(
                    max(float(data["confidence"]), 0.0),
                    confidence_cap_for(evidence_depth, critical_missing, self.settings),
                ),
                4,
            )
            risk = _forecast_risk(
                evidence_depth,
                confidence,
                critical_missing,
                _is_structurally_simple(inp.market, inp.resolution),
            )
            forecast = MarketForecast(
                estimated_probability=probability,
                confidence=confidence,
                evidence_depth=evidence_depth,
                forecast_risk=risk,
                forecast_summary=data["forecast_summary"],
                bull_case=data["bull_case"],
                bear_case=data["bear_case"],
                skeptic_notes=data["skeptic_notes"],
                key_assumptions=data["key_assumptions"],
                missing_info=data["missing_info"],
                what_would_change_mind=data["what_would_change_mind"],
                calibration_tags=data["calibration_tags"],
            )
            forecast.raw_response = data
            return forecast
        except Exception:
            logger.exception(
                "LLM forecast failed for %s; using template baseline fallback",
                inp.market.ticker,
            )
            fallback = await self._baseline.forecast(inp)
            fallback.skeptic_notes = [*fallback.skeptic_notes, FALLBACK_NOTE]
            fallback.calibration_tags = [*fallback.calibration_tags, "llm_error_fallback"]
            return fallback


def get_forecaster(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.enable_llm_forecasting:
        return LLMForecaster(settings)
    return TemplateBaselineForecaster(settings)


class ForecastingService:
    def __init__(self, forecaster=None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._explicit_forecaster = forecaster is not None
        self.forecaster = forecaster or get_forecaster(self.settings)

    def _forecaster_for(self, inp: ForecastInput):
        """Per-market forecaster selection. An explicitly injected forecaster
        always wins. The evidence canaries
        (ENABLE_BASEBALL_EVIDENCE_FORECASTING / SOCCER-002's
        ENABLE_SOCCER_EVIDENCE_FORECASTING) apply only to their domain with a
        source-backed, sufficiently complete packet and a researchable
        resolution; everything else keeps the configured default."""
        if self._explicit_forecaster:
            return self.forecaster
        if (
            self.settings.enable_baseball_evidence_forecasting
            and inp.domain == "sports_baseball"
            and determine_evidence_depth(inp.packet) == EVIDENCE_SOURCE_BACKED
            and (inp.packet.research_completeness_score or 0.0)
            >= self.settings.baseball_forecast_min_completeness
            and inp.resolution is not None
            and inp.resolution.tradeability == "researchable"
        ):
            from app.services.baseball_forecasting import BaseballEvidenceAwareForecaster

            return BaseballEvidenceAwareForecaster(self.settings)
        if (
            self.settings.enable_soccer_evidence_forecasting
            and inp.domain == "sports_soccer"
            and determine_evidence_depth(inp.packet) == EVIDENCE_SOURCE_BACKED
            and (inp.packet.research_completeness_score or 0.0)
            >= self.settings.soccer_forecast_min_completeness
            and inp.resolution is not None
            and inp.resolution.tradeability == "researchable"
        ):
            from app.services.soccer_forecasting import SoccerEvidenceAwareForecaster

            return SoccerEvidenceAwareForecaster(self.settings)
        if (
            self.settings.enable_tennis_evidence_forecasting
            and inp.domain == "sports_tennis"
            and determine_evidence_depth(inp.packet) == EVIDENCE_SOURCE_BACKED
            and (inp.packet.research_completeness_score or 0.0)
            >= self.settings.tennis_forecast_min_completeness
            and inp.resolution is not None
            and inp.resolution.tradeability == "researchable"
        ):
            from app.services.tennis_forecasting import TennisEvidenceAwareForecaster

            return TennisEvidenceAwareForecaster(self.settings)
        return self.forecaster

    async def forecast_market(
        self,
        session: Session,
        market: Market,
        scanner_run_id: int | None = None,
    ) -> MarketForecastRecord:
        """Build and persist one forecast from the market's latest research
        packet. Raises MissingResearchPacketError when no packet exists.

        Deterministic guarantees applied to every forecaster's output:
        evidence_depth is recomputed from the packet, confidence caps are
        enforced, and an 'avoid' resolution forces forecast_risk=high."""
        packet = latest_packet_for(session, market.ticker)
        if packet is None:
            raise MissingResearchPacketError(
                f"No research packet exists for {market.ticker!r}; "
                "run collect-research (or pass prepare) first"
            )
        resolution = latest_assessment_for(session, market.ticker)
        enrichment = latest_enrichment_for(session, market.ticker)
        market_data = apply_enrichment(market_data_from_row(market), enrichment)
        market_data = _apply_latest_snapshot(session, market, market_data)

        inp = ForecastInput(market=market_data, packet=packet, resolution=resolution)
        forecaster = self._forecaster_for(inp)
        forecast = await forecaster.forecast(inp)

        evidence_depth = determine_evidence_depth(packet)
        critical_missing = is_critical_info_missing(packet, resolution)
        confidence = round(
            min(forecast.confidence, confidence_cap_for(evidence_depth, critical_missing, self.settings)),
            4,
        )
        updates: dict = {"evidence_depth": evidence_depth, "confidence": confidence}
        if resolution is not None and resolution.tradeability == "avoid":
            updates["forecast_risk"] = "high"
        forecast = forecast.model_copy(update=updates)

        row = MarketForecastRecord(
            market_ticker=market.ticker,
            scanner_run_id=scanner_run_id,
            research_packet_id=packet.id,
            resolution_assessment_id=resolution.id if resolution else None,
            forecaster_name=forecaster.name,
            forecaster_version=forecaster.version,
            model_name=forecaster.model_name,
            prompt_version=getattr(forecaster, "prompt_version", "v1"),
            estimated_probability=forecast.estimated_probability,
            confidence=forecast.confidence,
            evidence_depth=forecast.evidence_depth,
            forecast_risk=forecast.forecast_risk,
            forecast_summary=forecast.forecast_summary,
            bull_case=forecast.bull_case.model_dump(),
            bear_case=forecast.bear_case.model_dump(),
            skeptic_notes=forecast.skeptic_notes,
            key_assumptions=[a.model_dump() for a in forecast.key_assumptions],
            missing_info=forecast.missing_info,
            what_would_change_mind=[t.model_dump() for t in forecast.what_would_change_mind],
            calibration_tags=forecast.calibration_tags,
            raw_response=forecast.raw_response or forecast.model_dump(exclude={"raw_response"}),
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.commit()
        return row


def latest_forecast_for(session: Session, ticker: str) -> MarketForecastRecord | None:
    from sqlalchemy import select

    return session.execute(
        select(MarketForecastRecord)
        .where(MarketForecastRecord.market_ticker == ticker)
        .order_by(MarketForecastRecord.created_at.desc(), MarketForecastRecord.id.desc())
    ).scalars().first()
