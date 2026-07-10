"""TENNIS-PROVIDER-001 — provider adapter SCAFFOLD (default-off, read-only).

Research (docs/TENNIS_PROVIDER_RESEARCH_2026_07_10.md) selected API-Tennis
(api-tennis.com) as the first bounded-validation candidate for the tiers our
universe actually needs (~79% ITF-family, ~15% Challenger — measured). This
module is the SCAFFOLD only:

- Without `TENNIS_PROVIDER_API_KEY` (default empty) the fetcher makes NO
  request and returns None — the caller reports provider_gap honestly.
- With a key AND `TENNIS_RESEARCH_PROVIDER=api_tennis`, `fetch_scoreboard`
  performs one read-only GET per date and adapts the response into the same
  internal scoreboard shape TENNIS-001's `_find_event` already matches
  against (events → competitors → athlete name/abbreviation, status).
- Kalshi matchup codes are derived as the last name's first three letters
  (CASAMB → CAS/AMB). Four-letter codes exist and will NOT match in v1 —
  the bounded validation run measures the real match rate; tuning is an
  expected, explicit iteration. Nothing is fabricated.

Read-only provider validation plumbing only: no probability models, no EV,
no recommendations, no persistence of provider data, no trading semantics of
any kind, and no MarketOps/EDGE-AUTO/forecast/gate/flag behavior change.
The API key is never logged or echoed — presence/absence only.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

API_TENNIS_BASE = "https://api.api-tennis.com/tennis/"
API_TENNIS_SOURCE = "api-tennis.com"

# fixture statuses the adapter passes through as the ESPN-shaped state
_LIVE_MARKERS = ("live", "in progress", "set", "1st", "2nd", "3rd", "4th", "5th")


def kalshi_code(name: str | None) -> str:
    """Best-effort Kalshi player code from a display name: the last name's
    first three letters, uppercased ('Jannik Sinner' -> 'SIN'). Empty when the
    name is missing — never guessed."""
    if not name or not name.strip():
        return ""
    return name.strip().split()[-1][:3].upper()


def _state_for(status_text: str) -> str:
    lowered = (status_text or "").lower()
    if not lowered or lowered in ("cancelled", "canceled", "postponed"):
        return "unknown"
    if lowered in ("finished", "retired", "walkover", "walk over"):
        return "post"
    if any(marker in lowered for marker in _LIVE_MARKERS):
        return "in"
    return "pre"


def adapt_fixtures_to_scoreboard(payload: dict | None, tour: str) -> dict | None:
    """Adapt an API-Tennis get_fixtures response into the internal scoreboard
    shape ({'events': [...]}) that tennis_research._find_event matches
    against. Fixtures missing either player name are skipped (honest), never
    padded. `tour` filters on the fixture's event_type when present
    ('atp' also admits Challenger/ITF men; 'wta' admits women/ITF women)."""
    if payload is None:
        return None
    fixtures = payload.get("result") or []
    events = []
    for f in fixtures:
        if not isinstance(f, dict):
            continue
        event_type = (f.get("event_type_type") or "").lower()
        if event_type and tour == "wta" and not any(
            k in event_type for k in ("wta", "girls", "women")
        ):
            continue
        if event_type and tour == "atp" and any(
            k in event_type for k in ("wta", "girls", "women")
        ):
            continue
        name_a = f.get("event_first_player")
        name_b = f.get("event_second_player")
        if not name_a or not name_b:
            continue
        status_text = f.get("event_status") or ""
        events.append({
            "competitions": [{
                "competitors": [
                    {"athlete": {
                        "displayName": name_a,
                        "abbreviation": kalshi_code(name_a),
                    }},
                    {"athlete": {
                        "displayName": name_b,
                        "abbreviation": kalshi_code(name_b),
                    }},
                ],
            }],
            "status": {"type": {
                "description": status_text or "unknown",
                "state": _state_for(status_text),
            }},
            "provider_meta": {
                "event_key": f.get("event_key"),
                "tournament": f.get("tournament_name"),
                "event_type": f.get("event_type_type"),
                "source": API_TENNIS_SOURCE,
            },
        })
    return {"events": events}


class ApiTennisFetcher:
    """Thin read-only client for api-tennis.com, presenting the same
    TennisDataFetcher protocol TENNIS-001 uses. Returns None on any failure
    or when no API key is configured — the caller falls back honestly."""

    source_name = API_TENNIS_SOURCE

    def __init__(self, api_key: str = "", timeout: float = 15.0):
        self._api_key = api_key or ""
        self.timeout = timeout

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def scoreboard_url(self, tour: str, date: str) -> str:
        # key intentionally omitted from the display URL — never echoed
        return f"{API_TENNIS_BASE}?method=get_fixtures&date_start={date}&date_stop={date}"

    def match_details_url(self, tour: str, event_id: str) -> str:
        return f"{API_TENNIS_BASE}?method=get_fixtures&match_key={event_id}"

    async def _get(self, params: dict) -> dict | None:
        if not self.has_key:
            logger.warning(
                "api_tennis provider selected but TENNIS_PROVIDER_API_KEY is "
                "absent — no request made (provider_gap)"
            )
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    API_TENNIS_BASE, params={**params, "APIkey": self._api_key}
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:  # network/HTTP/JSON — never raise upward
            logger.warning("api_tennis fetch failed: %s", type(exc).__name__)
            return None

    async def fetch_scoreboard(self, tour: str, date: str) -> dict | None:
        payload = await self._get({
            "method": "get_fixtures", "date_start": date, "date_stop": date,
        })
        return adapt_fixtures_to_scoreboard(payload, tour)

    async def fetch_match_details(self, tour: str, event_id: str) -> dict | None:
        payload = await self._get({"method": "get_fixtures", "match_key": event_id})
        if payload is None:
            return None
        result = payload.get("result") or []
        return result[0] if result else None
