"""Tennis external research canary (TENNIS-001).

Turns template_only research packets into source_backed packets for
sports_tennis MATCH-WINNER markets using live match evidence. Narrow by
design: SignalProcessingService uses this collector ONLY for promoted signals
whose domain is sports_tennis with a researchable resolution and the
ENABLE_TENNIS_EXTERNAL_RESEARCH flag on. The live data source is selected by
TENNIS_RESEARCH_PROVIDER: "template" (default) configures no fetcher, so the
collector always falls back honestly; "espn" selects a read-only public ESPN
tennis client whose payload mapping is PENDING validation against real ESPN
tennis responses — if the live shape differs it produces no usable evidence
and the collector still falls back honestly (evidence stays template_only).

Evidence gathered when available: match status, set score, current-set game
score, current server, winner / retirement / walkover, tournament, surface,
player rank/seed. Every fact carries a source reference; every source persists
url/title/type/credibility/freshness. When the match cannot be identified or
fetched, the collector falls back to the template packet honestly.

Read-only throughout; no EV, no trading semantics of any kind (no sizing, no
orders, no wallets/keys, no swaps, no signing, no execution).
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
ESPN_API_BASE = "https://site.api.espn.com/apis/site/v2/sports/tennis"

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Kalshi tennis series prefixes -> ESPN tour slugs (same prefixes that
# classify_domain uses for sports_tennis).
TOURS = {
    "KXATP": "atp",
    "KXWTA": "wta",
    "KXITF": "atp",  # ESPN groups most ITF/challenger events under atp scoreboards
}

# Best-effort Kalshi tennis ticker shape (mirrors the MLB/soccer shape):
#   KXATPMATCH-25MAY26DJOKALC   -> series MATCH, date, players DJOK/ALC
#   KXWTAWINNER-25MAY26-SWIGAU  -> series WINNER, date, players, optional suffix
# The time block and trailing suffix are optional. Only MATCH/WINNER/GAME
# series map to a winner market; everything else stays unknown (honest).
TENNIS_TICKER_RE = re.compile(
    r"^(KXATP|KXWTA|KXITF)([A-Z0-9]*)-(\d{2})([A-Z]{3})(\d{2})(?:\d{4})?([A-Z]{4,12})(?:-(.+))?$"
)

WINNER_MARKERS = ("MATCH", "WINNER", "WIN", "GAME")

# Template gaps this collector can close (see research.DOMAIN_TEMPLATES tennis).
GAP_RANKINGS = "player rankings and seedings"
GAP_SURFACE = "surface and conditions"

FALLBACK_PREFIX = "external research unavailable"


@dataclass(frozen=True)
class TennisMatchContext:
    date: str  # YYYY-MM-DD
    matchup: str  # concatenated player codes, e.g. "DJOKALC"
    tour: str  # ESPN tour slug, e.g. "atp"
    market_type: str  # winner | unknown (v1 supports winner only)
    player_a: str | None = None  # matchup halves when unambiguous (even length)
    player_b: str | None = None


def _market_type_for(series_fragment: str) -> str:
    if any(marker in series_fragment for marker in WINNER_MARKERS):
        return "winner"
    return "unknown"


def parse_tennis_ticker(ticker: str) -> TennisMatchContext | None:
    """Best-effort parse of a Kalshi tennis ticker. Returns None (honest
    unknown) whenever the shape does not match — the collector then falls back
    to the template packet. v1 recognizes match-winner series only."""
    match = TENNIS_TICKER_RE.match(ticker.upper())
    if not match:
        return None
    prefix, series, year, month_name, day, matchup, _suffix = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    player_a = player_b = None
    if len(matchup) % 2 == 0:
        half = len(matchup) // 2
        player_a, player_b = matchup[:half], matchup[half:]
    return TennisMatchContext(
        date=f"20{year}-{month:02d}-{int(day):02d}",
        matchup=matchup,
        tour=TOURS[prefix],
        market_type=_market_type_for(series),
        player_a=player_a,
        player_b=player_b,
    )


class TennisDataFetcher(Protocol):
    """Read-only source of live tennis match data. Implementations return None
    on any fetch failure — never raise into the collector."""

    source_name: str

    def scoreboard_url(self, tour: str, date: str) -> str: ...

    def match_details_url(self, tour: str, event_id: str) -> str: ...

    async def fetch_scoreboard(self, tour: str, date: str) -> dict | None: ...

    async def fetch_match_details(self, tour: str, event_id: str) -> dict | None: ...


class EspnTennisApiFetcher:
    """Thin read-only client for the public ESPN tennis API. Every method
    returns None on any HTTP/network error. PENDING: the exact ESPN tennis
    payload mapping is not yet validated against live responses; until then the
    collector degrades to honest template fallback when the shape does not
    match. No credentials, read-only GETs."""

    source_name = ESPN_SOURCE_NAME

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def scoreboard_url(self, tour: str, date: str) -> str:
        return f"{ESPN_API_BASE}/{tour}/scoreboard?dates={date.replace('-', '')}"

    def match_details_url(self, tour: str, event_id: str) -> str:
        return f"{ESPN_API_BASE}/{tour}/summary?event={event_id}"

    async def _get(self, url: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.warning("ESPN tennis API fetch failed for %s: %s", url, exc)
            return None

    async def fetch_scoreboard(self, tour: str, date: str) -> dict | None:
        return await self._get(self.scoreboard_url(tour, date))

    async def fetch_match_details(self, tour: str, event_id: str) -> dict | None:
        return await self._get(self.match_details_url(tour, event_id))


def get_tennis_fetcher(settings: Settings | None = None) -> TennisDataFetcher | None:
    """Provider-selected fetcher, or None for "template" (and unknown
    providers), which keeps the collector in honest fallback mode."""
    settings = settings or get_settings()
    provider = settings.tennis_research_provider.strip().lower()
    if provider == "espn":
        return EspnTennisApiFetcher(timeout=settings.tennis_research_timeout_seconds)
    if provider != "template":
        logger.warning("Unknown TENNIS_RESEARCH_PROVIDER %r; using template fallback", provider)
    return None


def _competitors(event: dict) -> list[dict]:
    competitions = event.get("competitions") or []
    if not competitions:
        return []
    return (competitions[0] or {}).get("competitors") or []


def _athlete_abbr(competitor: dict) -> str:
    athlete = competitor.get("athlete") or competitor.get("team") or {}
    return (athlete.get("abbreviation") or athlete.get("shortName") or "").upper().replace(" ", "")


def _find_event(scoreboard: dict, context: TennisMatchContext) -> dict | None:
    """Match the ticker's players against scoreboard competitors — as a set of
    abbreviations when the matchup splits cleanly, else as a concatenation in
    either order."""
    for event in scoreboard.get("events") or []:
        abbrs = [_athlete_abbr(c) for c in _competitors(event)]
        if len(abbrs) != 2 or not all(abbrs):
            continue
        if context.player_a and context.player_b:
            if {context.player_a, context.player_b} == set(abbrs):
                return event
        elif context.matchup in (abbrs[0] + abbrs[1], abbrs[1] + abbrs[0]):
            return event
    return None


def _extract_match_evidence(
    event: dict, details: dict | None, source_name: str
) -> tuple[list[ResearchFact], dict, set[str]]:
    """(facts, extracted-context-dict, filled-gap-names) from a scoreboard
    event plus (optional) match-details payload."""
    facts: list[ResearchFact] = []
    extracted: dict = {}
    filled: set[str] = set()

    competitors = _competitors(event)
    if len(competitors) != 2:
        return facts, extracted, filled

    names = {
        (c.get("order") if c.get("order") in (0, 1) else i): (
            (c.get("athlete") or c.get("team") or {}).get("displayName") or f"player{i}"
        )
        for i, c in enumerate(competitors)
    }
    status = event.get("status") or {}
    status_type = status.get("type") or {}
    status_text = status_type.get("description") or "unknown"
    state = status_type.get("state") or "unknown"
    extracted["status"] = status_text
    extracted["state"] = state

    a, b = competitors[0], competitors[1]
    name_a = (a.get("athlete") or a.get("team") or {}).get("displayName") or "player A"
    name_b = (b.get("athlete") or b.get("team") or {}).get("displayName") or "player B"

    # Set score (linescores) + current-set game score
    def linescores(c):
        return [ls.get("value") for ls in (c.get("linescores") or []) if ls.get("value") is not None]

    sets_a, sets_b = linescores(a), linescores(b)
    if sets_a or sets_b:
        # When live, the LAST linescore pair is the current (in-progress) set —
        # its games are not a won set yet. When final, all pairs are complete.
        pairs = list(zip(sets_a, sets_b))
        current_games = None
        if state == "in" and pairs:
            current_games = pairs[-1]
            completed = pairs[:-1]
        else:
            completed = pairs
        sets_won_a = sum(1 for x, y in completed if x is not None and y is not None and x > y)
        sets_won_b = sum(1 for x, y in completed if x is not None and y is not None and y > x)
        facts.append(
            ResearchFact(
                fact=f"Match state: {name_a} vs {name_b} — sets {sets_won_a}-{sets_won_b} "
                f"(games {sets_a} vs {sets_b}); {status_text}",
                confidence=0.95,
                source_name=source_name,
            )
        )
        extracted["sets"] = {"a": sets_won_a, "b": sets_won_b}
        if current_games is not None:
            extracted["games"] = {"a": current_games[0], "b": current_games[1]}
        elif sets_a and sets_b:
            extracted["games"] = {"a": sets_a[-1], "b": sets_b[-1]}

    # Winner / retirement / walkover
    winner_side = None
    if a.get("winner") is True:
        winner_side = "a"
    elif b.get("winner") is True:
        winner_side = "b"
    if winner_side or state == "post":
        detail_txt = status_type.get("detail") or status_text
        retired = "retire" in detail_txt.lower() or "walkover" in detail_txt.lower()
        facts.append(
            ResearchFact(
                fact=f"Result: {status_text}"
                + (f" — winner {name_a if winner_side == 'a' else name_b}" if winner_side else "")
                + (" (retirement/walkover)" if retired else ""),
                confidence=0.95,
                source_name=source_name,
            )
        )
        extracted["winner"] = winner_side
        extracted["retirement"] = retired

    # Current server (when live)
    situation = ((event.get("competitions") or [{}])[0] or {}).get("situation") or {}
    server = situation.get("server")
    if state == "in" and server is not None:
        extracted["server"] = server
        facts.append(
            ResearchFact(fact=f"Current server side: {server}", confidence=0.8, source_name=source_name)
        )

    # Tournament + surface (from event/competition metadata when present)
    tournament = (
        (event.get("season") or {}).get("displayName")
        or event.get("shortName")
        or ((event.get("competitions") or [{}])[0] or {}).get("notes")
    )
    if tournament:
        extracted["tournament"] = str(tournament)[:120]
        facts.append(
            ResearchFact(fact=f"Tournament: {tournament}", confidence=0.85, source_name=source_name)
        )
    comp0 = (event.get("competitions") or [{}])[0] or {}
    surface = (comp0.get("surface") or {}).get("name") if isinstance(comp0.get("surface"), dict) else comp0.get("surface")
    if surface:
        extracted["surface"] = str(surface)
        filled.add(GAP_SURFACE)
        facts.append(
            ResearchFact(fact=f"Surface: {surface}", confidence=0.85, source_name=source_name)
        )

    # Player rank / seed
    ranks = {}
    for side, c, nm in (("a", a, name_a), ("b", b, name_b)):
        rank = c.get("rank") or (c.get("athlete") or {}).get("rank")
        seed = c.get("seed") or (c.get("curatedRank") or {}).get("current")
        if rank or seed:
            ranks[side] = {"rank": rank, "seed": seed}
    if ranks:
        extracted["ranks"] = ranks
        filled.add(GAP_RANKINGS)
        facts.append(
            ResearchFact(
                fact="Rank/seed: " + "; ".join(
                    f"{name_a if s == 'a' else name_b} rank {v.get('rank')} seed {v.get('seed')}"
                    for s, v in ranks.items()
                ),
                confidence=0.8,
                source_name=source_name,
            )
        )

    return facts, extracted, filled


class TennisExternalResearchCollector:
    """Template scaffold + live tennis match evidence. Falls back to the
    template packet content (marked, template_only depth) when no fetcher is
    configured or the match cannot be identified or fetched."""

    def __init__(
        self,
        fetcher: TennisDataFetcher | None = None,
        settings: Settings | None = None,
    ):
        settings = settings or get_settings()
        self.name = "tennis-external"
        self.version = settings.tennis_research_collector_version
        self.max_sources = settings.tennis_research_max_sources
        self.fetcher = fetcher if fetcher is not None else get_tennis_fetcher(settings)
        self._template = TemplateResearchCollector()

    def _fallback(self, baseline: ResearchPacket, reason: str) -> ResearchPacket:
        logger.info("Tennis external research fallback: %s", reason)
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
            context = parse_tennis_ticker(market.ticker)
            if context is None:
                return self._fallback(baseline, "ticker not parseable as a tennis match")
            if context.market_type != "winner":
                return self._fallback(baseline, "v1 supports match-winner markets only")

            scoreboard = await self.fetcher.fetch_scoreboard(context.tour, context.date)
            if not scoreboard:
                return self._fallback(baseline, "scoreboard unavailable")
            event = _find_event(scoreboard, context)
            if event is None:
                return self._fallback(baseline, f"no scoreboard match for {context.matchup}")
            event_id = str(event.get("id") or "")
            details = (
                await self.fetcher.fetch_match_details(context.tour, event_id)
                if event_id
                else None
            )

            facts, extracted, filled = _extract_match_evidence(
                event, details, self.fetcher.source_name
            )
            if not facts:
                return self._fallback(baseline, "event contained no usable evidence")
            extracted["market_type"] = context.market_type

            fetched_at = datetime.now(timezone.utc).isoformat()
            external_sources = [
                ResearchSource(
                    name=f"{self.fetcher.source_name} — scoreboard",
                    url=self.fetcher.scoreboard_url(context.tour, context.date),
                    source_type="stats_provider",
                    confidence=0.85,
                    title=f"{context.tour} scoreboard {context.date}",
                    credibility="high",
                    fetched_at=fetched_at,
                ),
            ]
            if details is not None:
                external_sources.append(
                    ResearchSource(
                        name=f"{self.fetcher.source_name} — match details",
                        url=self.fetcher.match_details_url(context.tour, event_id),
                        source_type="stats_provider",
                        confidence=0.85,
                        title=f"Match details {event_id}",
                        credibility="high",
                        fetched_at=fetched_at,
                    )
                )

            score = baseline.research_completeness_score
            if "sets" in extracted:
                score += 0.15
            if "games" in extracted:
                score += 0.05
            if "winner" in extracted:
                score += 0.05
            if "surface" in extracted:
                score += 0.05
            if "ranks" in extracted:
                score += 0.10
            if "tournament" in extracted:
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
            logger.exception("Tennis external research failed for %s", market.ticker)
            return self._fallback(baseline, f"{type(exc).__name__}: {exc}")
