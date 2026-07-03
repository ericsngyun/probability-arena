"""Baseball external research canary (MVP-004E).

Turns template_only research packets into source_backed packets for
sports_baseball markets using the public MLB Stats API (statsapi.mlb.com —
official league data, read-only GETs, no credentials). Narrow by design:
SignalProcessingService uses this collector ONLY for promoted signals whose
domain is sports_baseball with a researchable resolution and the
ENABLE_BASEBALL_EXTERNAL_RESEARCH flag on.

Evidence gathered when available: game state (score / inning / outs / bases),
probable pitchers, confirmed lineups, weather/venue. Every fact carries a
source reference; every source persists url/title/type/credibility/freshness.
When nothing can be fetched, the collector falls back to the template packet
content honestly (evidence depth stays template_only) with the reason
recorded.

Read-only throughout; no EV, no trading semantics of any kind.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import MarketResearchPacket
from app.schemas import (
    CollectorStats,
    MarketData,
    ResearchCanaryReport,
    ResearchFact,
    ResearchPacket,
    ResearchSource,
)
from app.services.research import DOMAIN_GENERAL, TemplateResearchCollector, _risk_for

logger = logging.getLogger(__name__)

MLB_SOURCE_NAME = "statsapi.mlb.com"
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Kalshi MLB tickers embed the game: KXMLBTOTAL-26JUL021915STLATL-18
#   -> year 26, JUL 02, 19:15, matchup STLATL (away+home abbreviations)
MLB_TICKER_RE = re.compile(r"^KXMLB[A-Z0-9]*-(\d{2})([A-Z]{3})(\d{2})\d{4}([A-Z]{4,6})(?:-|$)")

# Template gaps that external facts can close (see research.DOMAIN_TEMPLATES)
GAP_LINEUP = "confirmed starting lineup"
GAP_PITCHERS = "probable pitcher matchup and handedness splits"
GAP_WEATHER = "ballpark and weather conditions"

FALLBACK_PREFIX = "external research unavailable"


@dataclass(frozen=True)
class MlbGameContext:
    date: str  # YYYY-MM-DD
    matchup: str  # concatenated away+home abbreviations, e.g. "STLATL"


def parse_mlb_ticker(ticker: str) -> MlbGameContext | None:
    match = MLB_TICKER_RE.match(ticker.upper())
    if not match:
        return None
    year, month_name, day, matchup = match.groups()
    month = MONTHS.get(month_name)
    if month is None:
        return None
    return MlbGameContext(date=f"20{year}-{month:02d}-{int(day):02d}", matchup=matchup)


class MlbStatsApiFetcher:
    """Thin read-only client for the public MLB Stats API. Every method
    returns None on any HTTP/network error."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    def schedule_url(self, date: str) -> str:
        return f"{MLB_API_BASE}/schedule?sportId=1&date={date}&hydrate=team,probablePitcher"

    def live_feed_url(self, game_pk: int) -> str:
        return f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"

    async def _get(self, url: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.warning("MLB Stats API fetch failed for %s: %s", url, exc)
            return None

    async def fetch_schedule(self, date: str) -> dict | None:
        return await self._get(self.schedule_url(date))

    async def fetch_live_feed(self, game_pk: int) -> dict | None:
        return await self._get(self.live_feed_url(game_pk))


def _find_game(schedule: dict, matchup: str) -> dict | None:
    for date_entry in schedule.get("dates") or []:
        for game in date_entry.get("games") or []:
            teams = game.get("teams") or {}
            away = ((teams.get("away") or {}).get("team") or {}).get("abbreviation") or ""
            home = ((teams.get("home") or {}).get("team") or {}).get("abbreviation") or ""
            if away and home and f"{away}{home}".upper() == matchup.upper():
                return game
    return None


def _extract_game_evidence(feed: dict) -> tuple[list[ResearchFact], dict, set[str]]:
    """(facts, extracted-context-dict, filled-gap-names) from a live feed."""
    facts: list[ResearchFact] = []
    extracted: dict = {}
    filled: set[str] = set()
    game_data = feed.get("gameData") or {}
    live_data = feed.get("liveData") or {}

    status = (game_data.get("status") or {}).get("detailedState") or "unknown"
    abstract = (game_data.get("status") or {}).get("abstractGameState") or "unknown"
    extracted["status"] = status
    away_name = (((game_data.get("teams") or {}).get("away")) or {}).get("name") or "away"
    home_name = (((game_data.get("teams") or {}).get("home")) or {}).get("name") or "home"

    linescore = live_data.get("linescore") or {}
    score_teams = linescore.get("teams") or {}
    away_runs = (score_teams.get("away") or {}).get("runs")
    home_runs = (score_teams.get("home") or {}).get("runs")
    if away_runs is not None and home_runs is not None:
        state_bits = [f"{away_name} {away_runs} — {home_name} {home_runs} ({status})"]
        if abstract == "Live":
            inning = linescore.get("currentInning")
            inning_state = linescore.get("inningState") or ""
            outs = linescore.get("outs")
            offense = linescore.get("offense") or {}
            bases = [base for base in ("first", "second", "third") if offense.get(base)]
            state_bits.append(
                f"{inning_state} {inning}, {outs} out(s), "
                f"bases: {'/'.join(bases) if bases else 'empty'}"
            )
        facts.append(
            ResearchFact(
                fact="Game state: " + "; ".join(state_bits),
                confidence=0.95,
                source_name=MLB_SOURCE_NAME,
            )
        )
        extracted["score"] = {"away": away_runs, "home": home_runs}
        extracted["abstract_state"] = abstract

    probables = game_data.get("probablePitchers") or {}
    away_pitcher = (probables.get("away") or {}).get("fullName")
    home_pitcher = (probables.get("home") or {}).get("fullName")
    if away_pitcher or home_pitcher:
        facts.append(
            ResearchFact(
                fact=f"Probable pitchers: {away_pitcher or 'TBD'} ({away_name}) vs "
                f"{home_pitcher or 'TBD'} ({home_name})",
                confidence=0.9,
                source_name=MLB_SOURCE_NAME,
            )
        )
        extracted["probable_pitchers"] = {"away": away_pitcher, "home": home_pitcher}
        filled.add(GAP_PITCHERS)

    boxscore_teams = ((live_data.get("boxscore") or {}).get("teams")) or {}
    lineups_confirmed = bool(
        (boxscore_teams.get("away") or {}).get("battingOrder")
        and (boxscore_teams.get("home") or {}).get("battingOrder")
    )
    if lineups_confirmed:
        facts.append(
            ResearchFact(
                fact="Confirmed lineups are posted for both teams",
                confidence=0.9,
                source_name=MLB_SOURCE_NAME,
            )
        )
        extracted["lineups_confirmed"] = True
        filled.add(GAP_LINEUP)

    weather = game_data.get("weather") or {}
    if weather.get("condition") or weather.get("temp"):
        venue = (game_data.get("venue") or {}).get("name") or "venue"
        facts.append(
            ResearchFact(
                fact=f"Weather at {venue}: {weather.get('condition', 'n/a')}, "
                f"{weather.get('temp', '?')}F, wind {weather.get('wind', 'n/a')}",
                confidence=0.8,
                source_name=MLB_SOURCE_NAME,
            )
        )
        extracted["weather"] = weather
        filled.add(GAP_WEATHER)

    return facts, extracted, filled


class BaseballExternalResearchCollector:
    """Template scaffold + real MLB Stats API evidence. Falls back to the
    template packet content (marked, template_only depth) when the game
    cannot be identified or fetched."""

    def __init__(self, fetcher: MlbStatsApiFetcher | None = None, settings: Settings | None = None):
        settings = settings or get_settings()
        self.name = "baseball-external"
        self.version = settings.baseball_research_collector_version
        self.max_sources = settings.baseball_research_max_sources
        self.fetcher = fetcher or MlbStatsApiFetcher(
            timeout=settings.baseball_research_timeout_seconds
        )
        self._template = TemplateResearchCollector()

    def _fallback(self, baseline: ResearchPacket, reason: str) -> ResearchPacket:
        logger.info("Baseball external research fallback: %s", reason)
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
            context = parse_mlb_ticker(market.ticker)
            if context is None:
                return self._fallback(baseline, "ticker not parseable as an MLB game")

            schedule = await self.fetcher.fetch_schedule(context.date)
            if not schedule:
                return self._fallback(baseline, "MLB schedule unavailable")
            game = _find_game(schedule, context.matchup)
            if game is None:
                return self._fallback(baseline, f"no schedule match for {context.matchup}")
            game_pk = game.get("gamePk")
            feed = await self.fetcher.fetch_live_feed(game_pk) if game_pk else None
            if not feed:
                return self._fallback(baseline, "live feed unavailable")

            facts, extracted, filled = _extract_game_evidence(feed)
            if not facts:
                return self._fallback(baseline, "feed contained no usable evidence")

            fetched_at = datetime.now(timezone.utc).isoformat()
            external_sources = [
                ResearchSource(
                    name="MLB Stats API — schedule",
                    url=self.fetcher.schedule_url(context.date),
                    source_type="official",
                    confidence=0.9,
                    title=f"MLB schedule {context.date}",
                    credibility="official",
                    fetched_at=fetched_at,
                ),
                ResearchSource(
                    name="MLB Stats API — live feed",
                    url=self.fetcher.live_feed_url(game_pk),
                    source_type="official",
                    confidence=0.9,
                    title=f"Game feed {game_pk}",
                    credibility="official",
                    fetched_at=fetched_at,
                ),
            ]

            score = baseline.research_completeness_score
            if "score" in extracted:
                score += 0.15
            if "probable_pitchers" in extracted:
                score += 0.10
            if extracted.get("lineups_confirmed"):
                score += 0.10
            if "weather" in extracted:
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
                "game_pk": game_pk,
                "extracted": extracted,
                "source_urls": [source.url for source in external_sources],
            }
            return packet
        except Exception as exc:
            logger.exception("Baseball external research failed for %s", market.ticker)
            return self._fallback(baseline, f"{type(exc).__name__}: {exc}")


def build_research_canary_report(session: Session) -> ResearchCanaryReport:
    """Aggregate persisted packets by collector/domain/evidence depth."""
    from app.services.forecasting import determine_evidence_depth

    packets = session.execute(select(MarketResearchPacket)).scalars().all()
    by_collector: dict[str, dict] = {}
    by_domain: dict[str, int] = {}
    fallbacks = 0
    for packet in packets:
        domain = packet.domain or DOMAIN_GENERAL
        by_domain[domain] = by_domain.get(domain, 0) + 1
        depth = determine_evidence_depth(packet)
        bucket = by_collector.setdefault(
            packet.collector_name, {"count": 0, "scores": [], "depths": {}}
        )
        bucket["count"] += 1
        bucket["scores"].append(packet.research_completeness_score or 0.0)
        bucket["depths"][depth] = bucket["depths"].get(depth, 0) + 1
        if packet.collector_name.startswith("baseball-external") and depth == "template_only":
            fallbacks += 1

    from sqlalchemy import func

    from app.models import MarketForecastRecord

    forecasts_by_forecaster = dict(
        session.execute(
            select(MarketForecastRecord.forecaster_name, func.count()).group_by(
                MarketForecastRecord.forecaster_name
            )
        ).all()
    )

    return ResearchCanaryReport(
        total_packets=len(packets),
        by_collector={
            name: CollectorStats(
                count=data["count"],
                mean_completeness=round(sum(data["scores"]) / data["count"], 4),
                by_evidence_depth=data["depths"],
            )
            for name, data in by_collector.items()
        },
        by_domain=by_domain,
        external_fallbacks=fallbacks,
        forecasts_by_forecaster=forecasts_by_forecaster,
    )
