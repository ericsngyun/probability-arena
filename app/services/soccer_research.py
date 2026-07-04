"""Soccer / World Cup external research canary (SOCCER-001).

Turns template_only research packets into source_backed packets for
sports_soccer markets using live match evidence. Narrow by design:
SignalProcessingService uses this collector ONLY for promoted signals whose
domain is sports_soccer with a researchable resolution and the
ENABLE_SOCCER_EXTERNAL_RESEARCH flag on. The live data source is selected by
SOCCER_RESEARCH_PROVIDER: "template" (default) configures no fetcher, so the
collector always falls back honestly; "espn" uses the public ESPN soccer API
(site.api.espn.com — read-only GETs, no credentials).

Evidence gathered when available: score, match clock/period, red cards,
penalty-shootout state, confirmed lineups, basic match stats. Every fact
carries a source reference; every source persists
url/title/type/credibility/freshness. When the match cannot be identified or
fetched, the collector falls back to the template packet content honestly
(evidence depth stays template_only) with the reason recorded.

Read-only throughout; no EV, no trading semantics of any kind.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx

from app.config import Settings, get_settings
from app.schemas import MarketData, ResearchFact, ResearchPacket, ResearchSource
from app.services.research import TemplateResearchCollector, _risk_for

logger = logging.getLogger(__name__)

ESPN_SOURCE_NAME = "site.api.espn.com"
ESPN_API_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Kalshi soccer series prefixes -> ESPN league slugs (same prefixes that
# classify_domain uses for sports_soccer)
LEAGUES = {
    "KXWC": "fifa.world",
    "KXUCL": "uefa.champions",
    "KXEPL": "eng.1",
    "KXMLS": "usa.1",
}

# Kalshi soccer tickers follow the same shape as MLB ones:
#   KXWCGAME-26JUN141800USAWAL      -> series GAME, date+time, matchup USAWAL
#   KXWCTOTAL-26JUN14USAWAL-2.5     -> series TOTAL, matchup, line suffix
# The time block is optional; the trailing suffix (strike/side) is optional.
SOCCER_TICKER_RE = re.compile(
    r"^(KXWC|KXUCL|KXEPL|KXMLS)([A-Z0-9]*)-(\d{2})([A-Z]{3})(\d{2})(?:\d{4})?([A-Z]{4,8})(?:-(.+))?$"
)

# Series-name fragments -> market type (best effort; unknown stays unknown)
MARKET_TYPE_MARKERS = (
    ("total", ("TOTAL", "GOALS")),
    ("spread", ("SPREAD", "HANDICAP", "HCAP")),
    ("winner", ("GAME", "MATCH", "WIN", "")),
)

# Template gap external facts can close (see research.DOMAIN_TEMPLATES)
GAP_LINEUPS = "confirmed lineups"

FALLBACK_PREFIX = "external research unavailable"


@dataclass(frozen=True)
class SoccerMatchContext:
    date: str  # YYYY-MM-DD
    matchup: str  # concatenated team abbreviations, e.g. "USAWAL"
    league: str  # ESPN league slug, e.g. "fifa.world"
    market_type: str  # total | spread | winner | unknown
    line: float | None  # threshold parsed from the ticker suffix, if numeric
    team_a: str | None = None  # matchup halves when unambiguous (even length)
    team_b: str | None = None


def _market_type_for(series_fragment: str) -> str:
    for market_type, markers in MARKET_TYPE_MARKERS:
        if any(marker in series_fragment for marker in markers):
            return market_type
    return "unknown"


def parse_soccer_ticker(ticker: str) -> SoccerMatchContext | None:
    """Best-effort parse of a Kalshi soccer ticker. Returns None (honest
    unknown) whenever the shape does not match — the collector then falls
    back to the template packet."""
    match = SOCCER_TICKER_RE.match(ticker.upper())
    if not match:
        return None
    prefix, series, year, month_name, day, matchup, suffix = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    line: float | None = None
    if suffix:
        try:
            line = float(suffix.lstrip("TB"))  # strike suffixes like T2.5 / 2
        except ValueError:
            line = None
    team_a = team_b = None
    if len(matchup) % 2 == 0:
        half = len(matchup) // 2
        team_a, team_b = matchup[:half], matchup[half:]
    return SoccerMatchContext(
        date=f"20{year}-{month:02d}-{int(day):02d}",
        matchup=matchup,
        league=LEAGUES[prefix],
        market_type=_market_type_for(series),
        line=line,
        team_a=team_a,
        team_b=team_b,
    )


class SoccerDataFetcher(Protocol):
    """Read-only source of live soccer match data. Implementations return
    None on any fetch failure — never raise into the collector."""

    source_name: str

    def scoreboard_url(self, league: str, date: str) -> str: ...

    def match_details_url(self, league: str, event_id: str) -> str: ...

    async def fetch_scoreboard(self, league: str, date: str) -> dict | None: ...

    async def fetch_match_details(self, league: str, event_id: str) -> dict | None: ...


class EspnSoccerApiFetcher:
    """Thin read-only client for the public ESPN soccer API. Every method
    returns None on any HTTP/network error."""

    source_name = ESPN_SOURCE_NAME

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def scoreboard_url(self, league: str, date: str) -> str:
        return f"{ESPN_API_BASE}/{league}/scoreboard?dates={date.replace('-', '')}"

    def match_details_url(self, league: str, event_id: str) -> str:
        return f"{ESPN_API_BASE}/{league}/summary?event={event_id}"

    async def _get(self, url: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.warning("ESPN soccer API fetch failed for %s: %s", url, exc)
            return None

    async def fetch_scoreboard(self, league: str, date: str) -> dict | None:
        return await self._get(self.scoreboard_url(league, date))

    async def fetch_match_details(self, league: str, event_id: str) -> dict | None:
        return await self._get(self.match_details_url(league, event_id))


def get_soccer_fetcher(settings: Settings | None = None) -> SoccerDataFetcher | None:
    """Provider-selected fetcher, or None for "template" (and unknown
    providers), which keeps the collector in honest fallback mode."""
    settings = settings or get_settings()
    provider = settings.soccer_research_provider.strip().lower()
    if provider == "espn":
        return EspnSoccerApiFetcher(timeout=settings.soccer_research_timeout_seconds)
    if provider != "template":
        logger.warning("Unknown SOCCER_RESEARCH_PROVIDER %r; using template fallback", provider)
    return None


def _competitors(event: dict) -> list[dict]:
    competitions = event.get("competitions") or []
    if not competitions:
        return []
    return (competitions[0] or {}).get("competitors") or []


def _abbreviation(competitor: dict) -> str:
    return ((competitor.get("team") or {}).get("abbreviation") or "").upper()


def _find_event(scoreboard: dict, context: SoccerMatchContext) -> dict | None:
    """Match the ticker's teams against scoreboard competitors — as a set of
    abbreviations when the matchup splits cleanly, else as a concatenation in
    either order."""
    for event in scoreboard.get("events") or []:
        abbrs = [_abbreviation(c) for c in _competitors(event)]
        if len(abbrs) != 2 or not all(abbrs):
            continue
        if context.team_a and context.team_b:
            if {context.team_a, context.team_b} == set(abbrs):
                return event
        elif context.matchup in (abbrs[0] + abbrs[1], abbrs[1] + abbrs[0]):
            return event
    return None


STAT_ALLOWLIST = ("possessionPct", "totalShots", "shotsOnTarget", "wonCorners")


def _extract_match_evidence(
    event: dict, details: dict | None, source_name: str
) -> tuple[list[ResearchFact], dict, set[str]]:
    """(facts, extracted-context-dict, filled-gap-names) from a scoreboard
    event plus (optional) match-details payload."""
    facts: list[ResearchFact] = []
    extracted: dict = {}
    filled: set[str] = set()

    competitors = _competitors(event)
    names = {
        (c.get("homeAway") or f"side{i}"): ((c.get("team") or {}).get("displayName") or "team")
        for i, c in enumerate(competitors)
    }
    status = event.get("status") or {}
    status_type = status.get("type") or {}
    status_text = status_type.get("description") or "unknown"
    state = status_type.get("state") or "unknown"
    extracted["status"] = status_text
    extracted["state"] = state

    # Score + match clock/period (one combined match-state fact)
    scores = {c.get("homeAway") or "": c.get("score") for c in competitors}
    if len(competitors) == 2 and all(c.get("score") is not None for c in competitors):
        home, away = names.get("home", "home"), names.get("away", "away")
        state_bits = [f"{home} {scores.get('home')} — {away} {scores.get('away')} ({status_text})"]
        clock = status.get("displayClock")
        period = status.get("period")
        if state == "in" and clock:
            state_bits.append(f"clock {clock}, period {period}")
            extracted["clock"] = clock
            extracted["period"] = period
        facts.append(
            ResearchFact(
                fact="Match state: " + "; ".join(state_bits),
                confidence=0.95,
                source_name=source_name,
            )
        )
        extracted["score"] = {"home": scores.get("home"), "away": scores.get("away")}

    # Red cards from the competition play-by-play details, when present
    competition = (event.get("competitions") or [{}])[0] or {}
    play_details = competition.get("details")
    if isinstance(play_details, list):
        red_cards = [
            entry for entry in play_details if entry.get("redCard") is True
        ]
        descriptions = [
            f"{((entry.get('team') or {}).get('displayName') or 'unknown team')} "
            f"({(entry.get('clock') or {}).get('displayValue') or '?'})"
            for entry in red_cards
        ]
        facts.append(
            ResearchFact(
                fact="Red cards: " + ("; ".join(descriptions) if descriptions else "none shown"),
                confidence=0.9,
                source_name=source_name,
            )
        )
        extracted["red_cards"] = len(red_cards)

    # Penalty shootout state (final-on-pens status or shootout scores)
    shootout = {
        c.get("homeAway") or "": c.get("shootoutScore")
        for c in competitors
        if c.get("shootoutScore") is not None
    }
    if shootout or "PEN" in (status_type.get("name") or "").upper():
        pens = (
            f" ({names.get('home', 'home')} {shootout.get('home', '?')} — "
            f"{names.get('away', 'away')} {shootout.get('away', '?')})"
            if shootout
            else ""
        )
        facts.append(
            ResearchFact(
                fact=f"Penalty shootout involved: {status_text}{pens}",
                confidence=0.9,
                source_name=source_name,
            )
        )
        extracted["shootout"] = shootout or True

    # Confirmed lineups from the match-details rosters
    rosters = (details or {}).get("rosters") or []
    if len(rosters) >= 2 and all((r or {}).get("roster") for r in rosters):
        facts.append(
            ResearchFact(
                fact="Confirmed lineups are posted for both teams",
                confidence=0.9,
                source_name=source_name,
            )
        )
        extracted["lineups_confirmed"] = True
        filled.add(GAP_LINEUPS)

    # Basic match stats (allowlisted, both sides)
    stats_bits = []
    for stat_name in STAT_ALLOWLIST:
        values = {}
        for competitor in competitors:
            for stat in competitor.get("statistics") or []:
                if stat.get("name") == stat_name and stat.get("displayValue") is not None:
                    values[competitor.get("homeAway") or ""] = stat["displayValue"]
        if len(values) == 2:
            stats_bits.append(f"{stat_name} {values.get('home')}–{values.get('away')}")
    if stats_bits:
        facts.append(
            ResearchFact(
                fact="Match stats (home–away): " + ", ".join(stats_bits),
                confidence=0.85,
                source_name=source_name,
            )
        )
        extracted["stats"] = stats_bits

    return facts, extracted, filled


class SoccerExternalResearchCollector:
    """Template scaffold + live soccer match evidence. Falls back to the
    template packet content (marked, template_only depth) when no fetcher is
    configured or the match cannot be identified or fetched."""

    def __init__(
        self,
        fetcher: SoccerDataFetcher | None = None,
        settings: Settings | None = None,
    ):
        settings = settings or get_settings()
        self.name = "soccer-external"
        self.version = settings.soccer_research_collector_version
        self.max_sources = settings.soccer_research_max_sources
        self.fetcher = fetcher if fetcher is not None else get_soccer_fetcher(settings)
        self._template = TemplateResearchCollector()

    def _fallback(self, baseline: ResearchPacket, reason: str) -> ResearchPacket:
        logger.info("Soccer external research fallback: %s", reason)
        packet = baseline.model_copy(
            update={"missing_info": [*baseline.missing_info, f"{FALLBACK_PREFIX} ({reason})"]}
        )
        packet.raw_response = {"fallback": True, "reason": reason}
        return packet

    async def collect(
        self,
        market: MarketData,
        domain: str,
        resolution_tradeability: str | None = None,
    ) -> ResearchPacket:
        baseline = await self._template.collect(market, domain, resolution_tradeability)
        try:
            if self.fetcher is None:
                return self._fallback(
                    baseline, "provider is 'template' (no live fetcher configured)"
                )
            context = parse_soccer_ticker(market.ticker)
            if context is None:
                return self._fallback(baseline, "ticker not parseable as a soccer match")

            scoreboard = await self.fetcher.fetch_scoreboard(context.league, context.date)
            if not scoreboard:
                return self._fallback(baseline, "scoreboard unavailable")
            event = _find_event(scoreboard, context)
            if event is None:
                return self._fallback(baseline, f"no scoreboard match for {context.matchup}")
            event_id = str(event.get("id") or "")
            details = (
                await self.fetcher.fetch_match_details(context.league, event_id)
                if event_id
                else None
            )

            facts, extracted, filled = _extract_match_evidence(
                event, details, self.fetcher.source_name
            )
            if not facts:
                return self._fallback(baseline, "event contained no usable evidence")
            extracted["market_type"] = context.market_type
            if context.line is not None:
                extracted["line"] = context.line

            fetched_at = datetime.now(timezone.utc).isoformat()
            external_sources = [
                ResearchSource(
                    name=f"{self.fetcher.source_name} — scoreboard",
                    url=self.fetcher.scoreboard_url(context.league, context.date),
                    source_type="stats_provider",
                    confidence=0.85,
                    title=f"{context.league} scoreboard {context.date}",
                    credibility="high",
                    fetched_at=fetched_at,
                ),
            ]
            if details is not None:
                external_sources.append(
                    ResearchSource(
                        name=f"{self.fetcher.source_name} — match details",
                        url=self.fetcher.match_details_url(context.league, event_id),
                        source_type="stats_provider",
                        confidence=0.85,
                        title=f"Match details {event_id}",
                        credibility="high",
                        fetched_at=fetched_at,
                    )
                )

            score = baseline.research_completeness_score
            if "score" in extracted:
                score += 0.15
            if "clock" in extracted:
                score += 0.05
            if "red_cards" in extracted:
                score += 0.05
            if "shootout" in extracted:
                score += 0.05
            if extracted.get("lineups_confirmed"):
                score += 0.10
            if "stats" in extracted:
                score += 0.05
            score = round(min(score, 1.0), 4)

            missing_info = [gap for gap in baseline.missing_info if gap not in filled]

            packet = ResearchPacket(
                domain=domain,
                source_queries=baseline.source_queries,
                sources=(external_sources + baseline.sources)[: self.max_sources],
                key_facts=facts + baseline.key_facts,
                missing_info=missing_info,
                research_completeness_score=score,
                research_risk=_risk_for(score, resolution_tradeability),
            )
            packet.raw_response = {
                "fallback": False,
                "event_id": event_id,
                "extracted": extracted,
                "source_urls": [source.url for source in external_sources],
            }
            return packet
        except Exception as exc:
            logger.exception("Soccer external research failed for %s", market.ticker)
            return self._fallback(baseline, f"{type(exc).__name__}: {exc}")
