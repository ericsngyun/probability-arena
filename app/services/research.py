"""Research packet collection: structured evidence gathering for enriched,
researchable markets.

Collectors implement `async collect(market, domain, resolution_tradeability)
-> ResearchPacket`:

- TemplateResearchCollector — deterministic, domain-templated queries and
  expected sources; never touches the web. The default.
- MockResearchCollector — canned packets for tests.
- LLMWebResearchCollector — optional (ENABLE_EXTERNAL_RESEARCH=true); refines
  the template baseline with a Claude web-search call and falls back to the
  baseline on any error. Never required by tests.

Hard boundary for this layer: packets contain research inputs and facts only —
no probability forecasts, no position sizing, no trade recommendations.
"""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Market, MarketResearchPacket
from app.schemas import MarketData, ResearchFact, ResearchPacket, ResearchSource
from app.services.enrichment import apply_enrichment, latest_enrichment_for
from app.services.resolution import latest_assessment_for

logger = logging.getLogger(__name__)

DOMAIN_SPORTS_BASEBALL = "sports_baseball"
DOMAIN_SPORTS_TENNIS = "sports_tennis"
DOMAIN_SPORTS_SOCCER = "sports_soccer"
DOMAIN_MACRO = "macro"
DOMAIN_WEATHER = "weather"
DOMAIN_POLITICS = "politics"
DOMAIN_CRYPTO = "crypto"
DOMAIN_GENERAL = "general"

# Checked in order; first match wins. Ticker markers are prefixes/fragments,
# keywords match against title + category + settlement source (lowercased).
DOMAIN_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        DOMAIN_SPORTS_BASEBALL,
        ("KXMLB",),
        ("mlb", "baseball", "pitcher", "home run", "rbis", "innings"),
    ),
    (
        DOMAIN_SPORTS_TENNIS,
        ("KXATP", "KXWTA", "KXITF"),
        ("tennis", "atp", "wta", "itf", "grand slam", "wimbledon"),
    ),
    (
        DOMAIN_SPORTS_SOCCER,
        ("KXWC", "KXUCL", "KXEPL", "KXMLS"),
        ("soccer", "fifa", "uefa", "world cup", "premier league", "goals scored"),
    ),
    (
        DOMAIN_MACRO,
        ("KXFED", "KXCPI", "KXGDP", "KXPAYROLL"),
        ("federal funds", "fed ", "cpi", "inflation", "gdp", "unemployment", "payroll",
         "interest rate", "economics"),
    ),
    (
        DOMAIN_WEATHER,
        ("KXHIGH", "KXRAIN", "KXSNOW", "KXHURRICANE"),
        ("temperature", "rainfall", "snowfall", "hurricane", "weather", "noaa", "climate"),
    ),
    (
        DOMAIN_POLITICS,
        ("KXPRES", "KXSENATE", "KXHOUSE", "KXGOV"),
        ("election", "president", "senate", "congress", "governor", "politics", "nominee"),
    ),
    (
        DOMAIN_CRYPTO,
        ("KXBTC", "KXETH", "KXCRYPTO"),
        ("bitcoin", "btc", "ethereum", "eth", "crypto", "solana"),
    ),
)

# Per-domain query templates ({title} is substituted) and known info gaps a
# template packet cannot fill without external research.
DOMAIN_TEMPLATES: dict[str, dict[str, list[str]]] = {
    DOMAIN_SPORTS_BASEBALL: {
        "queries": [
            "{title} starting lineup confirmation",
            "{title} probable pitcher matchup",
            "player recent form last 10 games {title}",
            "{title} injury report",
            "ballpark weather forecast {title}",
        ],
        "missing_info": [
            "confirmed starting lineup",
            "probable pitcher matchup and handedness splits",
            "player form over the last 10 games",
            "injury/rest-day status",
            "ballpark and weather conditions",
        ],
        "expected_sources": [
            ("MLB.com stats", "https://www.mlb.com/stats", "stats_provider"),
            ("ESPN MLB", "https://www.espn.com/mlb/", "stats_provider"),
            ("Baseball Savant", "https://baseballsavant.mlb.com", "stats_provider"),
        ],
    },
    DOMAIN_SPORTS_TENNIS: {
        "queries": [
            "{title} head to head record",
            "{title} recent match results",
            "player ranking and surface form {title}",
            "{title} injury or retirement news",
        ],
        "missing_info": [
            "head-to-head record",
            "recent form and surface-specific results",
            "current ranking and seeding",
            "injury/withdrawal news",
        ],
        "expected_sources": [
            ("ATP Tour", "https://www.atptour.com", "stats_provider"),
            ("ITF", "https://www.itftennis.com", "official"),
            ("Flashscore tennis", "https://www.flashscore.com/tennis/", "stats_provider"),
        ],
    },
    DOMAIN_SPORTS_SOCCER: {
        "queries": [
            "{title} confirmed lineups",
            "{title} team news and injuries",
            "recent form and goal statistics {title}",
            "{title} referee and weather",
        ],
        "missing_info": [
            "confirmed lineups",
            "team news and suspensions",
            "recent form and goals for/against",
            "match conditions",
        ],
        "expected_sources": [
            ("FIFA", "https://www.fifa.com", "official"),
            ("ESPN FC", "https://www.espn.com/soccer/", "stats_provider"),
            ("Flashscore football", "https://www.flashscore.com", "stats_provider"),
        ],
    },
    DOMAIN_MACRO: {
        "queries": [
            "{title} consensus forecast",
            "latest data release {title}",
            "fed officials recent statements",
            "{title} nowcast estimate",
        ],
        "missing_info": [
            "consensus forecast",
            "most recent data release and revisions",
            "relevant official communications",
            "nowcast estimates",
        ],
        "expected_sources": [
            ("Federal Reserve", "https://www.federalreserve.gov", "official"),
            ("BLS", "https://www.bls.gov", "official"),
            ("FRED", "https://fred.stlouisfed.org", "stats_provider"),
        ],
    },
    DOMAIN_WEATHER: {
        "queries": [
            "{title} NWS forecast",
            "{title} model guidance GFS ECMWF",
            "historical climatology {title}",
        ],
        "missing_info": [
            "latest official forecast",
            "model guidance spread",
            "historical base rates for the date/location",
        ],
        "expected_sources": [
            ("National Weather Service", "https://www.weather.gov", "official"),
            ("NOAA", "https://www.noaa.gov", "official"),
        ],
    },
    DOMAIN_POLITICS: {
        "queries": [
            "{title} latest polling",
            "{title} candidate news",
            "prediction market and polling aggregate {title}",
        ],
        "missing_info": [
            "recent high-quality polling",
            "candidate/campaign news",
            "procedural or legal developments",
        ],
        "expected_sources": [
            ("AP News", "https://apnews.com", "news"),
            ("270toWin", "https://www.270towin.com", "stats_provider"),
        ],
    },
    DOMAIN_CRYPTO: {
        "queries": [
            "{title} current price and volatility",
            "crypto market news today",
            "{title} on-chain and derivatives data",
        ],
        "missing_info": [
            "current spot price and realized volatility",
            "notable market-moving news",
            "derivatives positioning",
        ],
        "expected_sources": [
            ("CoinGecko", "https://www.coingecko.com", "stats_provider"),
            ("Coinbase", "https://www.coinbase.com", "data_feed"),
        ],
    },
    DOMAIN_GENERAL: {
        "queries": [
            "{title} latest news",
            "{title} official announcements",
        ],
        "missing_info": [
            "domain identification (generic templates used)",
            "authoritative data source for the outcome",
        ],
        "expected_sources": [
            ("AP News", "https://apnews.com", "news"),
        ],
    },
}

RISK_LOW_MIN = 0.55
RISK_MEDIUM_MIN = 0.35


def classify_domain(market: MarketData) -> str:
    """Deterministic domain classification from ticker markers first, then
    keywords in title/category/settlement source."""
    ticker = market.ticker.upper()
    text = " ".join(
        filter(None, (market.title, market.category, market.settlement_source))
    ).lower()
    for domain, ticker_markers, keywords in DOMAIN_RULES:
        if any(ticker.startswith(marker) for marker in ticker_markers):
            return domain
    for domain, ticker_markers, keywords in DOMAIN_RULES:
        if any(keyword in text for keyword in keywords):
            return domain
    return DOMAIN_GENERAL


def _risk_for(score: float, resolution_tradeability: str | None) -> str:
    if resolution_tradeability == "avoid":
        return "high"
    if score >= RISK_LOW_MIN:
        return "low"
    if score >= RISK_MEDIUM_MIN:
        return "medium"
    return "high"


class TemplateResearchCollector:
    """Deterministic packet scaffold from domain templates. Never touches the
    web — completeness reflects what is knowable from local metadata alone."""

    def __init__(self, name: str | None = None, version: str | None = None):
        settings = get_settings()
        self.name = name or settings.research_collector_name
        self.version = version or settings.research_collector_version

    async def collect(
        self,
        market: MarketData,
        domain: str,
        resolution_tradeability: str | None = None,
    ) -> ResearchPacket:
        template = DOMAIN_TEMPLATES.get(domain, DOMAIN_TEMPLATES[DOMAIN_GENERAL])
        title = market.title or market.ticker

        queries = [query.format(title=title) for query in template["queries"]]
        missing_info = list(template["missing_info"])

        sources: list[ResearchSource] = []
        if market.settlement_source:
            for entry in market.settlement_source.split("; "):
                name, _, url = entry.partition(" (")
                sources.append(
                    ResearchSource(
                        name=name.strip() or entry,
                        url=url.rstrip(")") or None,
                        source_type="settlement_source",
                        confidence=0.9,
                    )
                )
        for name, url, source_type in template["expected_sources"]:
            sources.append(
                ResearchSource(name=name, url=url, source_type=source_type, confidence=0.5)
            )

        key_facts: list[ResearchFact] = []
        if market.settlement_source:
            key_facts.append(
                ResearchFact(
                    fact=f"Market settles via: {market.settlement_source}",
                    confidence=0.95,
                    source_name="kalshi_series_metadata",
                )
            )
        else:
            missing_info.append("settlement source unresolved")
        if market.close_time:
            key_facts.append(
                ResearchFact(
                    fact=f"Market closes at {market.close_time.isoformat()}",
                    confidence=1.0,
                    source_name="kalshi_market_metadata",
                )
            )
        if market.rules_primary:
            key_facts.append(
                ResearchFact(
                    fact=f"Resolution rule: {market.rules_primary[:300]}",
                    confidence=1.0,
                    source_name="kalshi_rules_text",
                )
            )

        score = 0.10
        if market.settlement_source:
            score += 0.20
        if market.rules_primary:
            score += 0.15
        if market.close_time:
            score += 0.05
        if resolution_tradeability == "researchable":
            score += 0.10
        if domain != DOMAIN_GENERAL:
            score += 0.05
        score = round(min(score, 1.0), 4)

        return ResearchPacket(
            domain=domain,
            source_queries=queries,
            sources=sources,
            key_facts=key_facts,
            missing_info=missing_info,
            research_completeness_score=score,
            research_risk=_risk_for(score, resolution_tradeability),
        )


class MockResearchCollector:
    """Canned packets for tests; records the tickers it was asked about."""

    name = "mock"
    version = "v1"

    def __init__(self, packet: ResearchPacket | None = None):
        self.packet = packet or ResearchPacket(
            domain=DOMAIN_GENERAL,
            source_queries=["mock query"],
            sources=[ResearchSource(name="Mock Source", source_type="web", confidence=0.5)],
            key_facts=[ResearchFact(fact="mock fact", confidence=0.5)],
            missing_info=["mock gap"],
            research_completeness_score=0.5,
            research_risk="medium",
        )
        self.collected_tickers: list[str] = []

    async def collect(self, market, domain, resolution_tradeability=None) -> ResearchPacket:
        self.collected_tickers.append(market.ticker)
        return self.packet


RESEARCH_SYSTEM_PROMPT_V1 = """You are a research assistant compiling an
evidence packet for a prediction market. Gather verifiable facts relevant to
the market's outcome using web search. Report facts with confidence and
provenance, list what remains unknown, and stop there — do NOT estimate
probabilities, recommend trades, or suggest positions."""

RESEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "source_queries": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "url": {"type": ["string", "null"]},
                    "source_type": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["name", "url", "source_type", "confidence"],
                "additionalProperties": False,
            },
        },
        "key_facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_name": {"type": ["string", "null"]},
                },
                "required": ["fact", "confidence", "source_name"],
                "additionalProperties": False,
            },
        },
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "research_completeness_score": {"type": "number"},
    },
    "required": [
        "source_queries",
        "sources",
        "key_facts",
        "missing_info",
        "research_completeness_score",
    ],
    "additionalProperties": False,
}


class LLMWebResearchCollector:
    """Optional Claude + web-search collector. Builds the template baseline
    first and falls back to it (flagged in missing_info) on any failure, so
    the pipeline never hard-fails because of the LLM."""

    def __init__(self, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = "llm-web"
        self.version = settings.research_collector_version
        self.model_name = settings.research_model_name
        self._baseline = TemplateResearchCollector()

    async def collect(
        self,
        market: MarketData,
        domain: str,
        resolution_tradeability: str | None = None,
    ) -> ResearchPacket:
        baseline = await self._baseline.collect(market, domain, resolution_tradeability)
        try:
            import anthropic

            client = anthropic.AsyncAnthropic()
            response = await client.messages.create(
                model=self.model_name,
                max_tokens=8192,
                system=RESEARCH_SYSTEM_PROMPT_V1,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
                output_config={
                    "format": {"type": "json_schema", "schema": RESEARCH_OUTPUT_SCHEMA}
                },
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "market": {
                                    "ticker": market.ticker,
                                    "title": market.title,
                                    "rules": market.rules_primary,
                                    "settlement_source": market.settlement_source,
                                    "close_time": market.close_time.isoformat()
                                    if market.close_time
                                    else None,
                                },
                                "domain": domain,
                                "suggested_queries": baseline.source_queries,
                                "known_gaps": baseline.missing_info,
                            },
                            default=str,
                        ),
                    }
                ],
            )
            if response.stop_reason == "refusal":
                raise RuntimeError("LLM refused the research request")
            text = next(block.text for block in response.content if block.type == "text")
            data = json.loads(text)
            score = round(min(max(float(data["research_completeness_score"]), 0.0), 1.0), 4)
            packet = ResearchPacket(
                domain=domain,  # classification stays deterministic
                source_queries=data["source_queries"],
                sources=data["sources"],
                key_facts=data["key_facts"],
                missing_info=data["missing_info"],
                research_completeness_score=score,
                research_risk=_risk_for(score, resolution_tradeability),
            )
            packet.raw_response = data
            return packet
        except Exception:
            logger.exception(
                "External research failed for %s; using template fallback", market.ticker
            )
            baseline.missing_info = [
                *baseline.missing_info,
                "external research unavailable (llm_error_fallback)",
            ]
            return baseline


def get_collector(settings: Settings | None = None):
    settings = settings or get_settings()
    if settings.enable_external_research:
        return LLMWebResearchCollector(settings)
    return TemplateResearchCollector()


def market_data_from_row(market: Market) -> MarketData:
    return MarketData(
        ticker=market.ticker,
        event_ticker=market.event_ticker,
        title=market.title or "",
        category=market.category,
        status=market.status,
        close_time=market.close_time,
        expiration_time=market.expiration_time,
        rules_primary=market.rules_primary,
    )


async def create_research_packet(
    session: Session,
    market: Market,
    collector=None,
    scanner_run_id: int | None = None,
) -> MarketResearchPacket:
    """Build and persist one research packet for a known market, linked to the
    latest enrichment and resolution assessment when they exist. Markets whose
    latest resolution says 'avoid' still get a packet, but always at
    research_risk=high."""
    collector = collector or get_collector()

    enrichment = latest_enrichment_for(session, market.ticker)
    resolution = latest_assessment_for(session, market.ticker)
    market_data = apply_enrichment(market_data_from_row(market), enrichment)
    domain = classify_domain(market_data)
    tradeability = resolution.tradeability if resolution else None

    packet = await collector.collect(market_data, domain, resolution_tradeability=tradeability)
    if tradeability == "avoid" and packet.research_risk != "high":
        # Service-level guarantee, independent of collector behavior
        packet = packet.model_copy(update={"research_risk": "high"})

    row = MarketResearchPacket(
        market_ticker=market.ticker,
        scanner_run_id=scanner_run_id,
        enrichment_id=enrichment.id if enrichment else None,
        resolution_assessment_id=resolution.id if resolution else None,
        collector_name=collector.name,
        collector_version=collector.version,
        domain=packet.domain,
        source_queries=packet.source_queries,
        sources=[source.model_dump() for source in packet.sources],
        key_facts=[fact.model_dump() for fact in packet.key_facts],
        missing_info=packet.missing_info,
        research_completeness_score=packet.research_completeness_score,
        research_risk=packet.research_risk,
        raw_response=packet.raw_response or packet.model_dump(exclude={"raw_response"}),
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row
