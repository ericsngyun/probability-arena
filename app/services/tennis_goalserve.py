"""TENNIS-GOALSERVE-001 — Goalserve fallback live-state VALIDATION (bounded).

API-Tennis is now proven catalog/mapping-only for our universe: REST
get_livescore returned 0 rows across 25+ probes, the WebSocket emitted 0
frames in a decisive 180s in-play probe, and fixture status stayed frozen —
all while Kalshi ITF/Challenger books repriced heavily. Per the
pre-registered fallback plan (docs/TENNIS_PROVIDER_RESEARCH_2026_07_10.md
§7c), this module tests Goalserve's falsifiable claim — "point-by-point every
5 seconds for all ATP/WTA/Challenger/ITF" — under the exact same conditions:
same live Kalshi candidates, same tape linker, bounded calls, no persistence.

Key handling is stricter than usual because **Goalserve embeds the key in the
URL path**: request URLs are never logged, echoed, or stored; reports carry a
masked display URL only; fetch failures surface as exception type names.

VALIDATION ONLY: no probability/Markov models, EV, paper trading,
recommendations, sizing, orders, wallets, signing, swaps, execution, or
autonomy — and no forecast/gate/promotion/MarketOps/EDGE-AUTO change.
Persists nothing.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.services.tennis_live_source import classify_tennis_market
from app.services.tennis_research import parse_tennis_ticker
from app.services.tennis_tape import LINK_SOURCE_BACKED, link_candidate
from app.services.tennis_watcher import discover_tennis_universe

logger = logging.getLogger(__name__)

GOALSERVE_NOTE = (
    "Bounded read-only Goalserve live-state validation under the same "
    "conditions that failed API-Tennis: same live Kalshi candidates, same "
    "linker, hard call cap, nothing persisted, key never in any printed URL. "
    "Coverage measurement only — not a model, not EV, not trading, never "
    "advice."
)

GOALSERVE_BASE = "https://www.goalserve.com/getfeed"
LIVE_PATH = "tennis_scores/live"
DISPLAY_URL = f"{GOALSERVE_BASE}/<key-redacted>/{LIVE_PATH}?json=1"
MAX_CALLS = 10                  # hard cap per validation run
MAX_PROBES = 8

VERDICT_PASS = "goalserve_pass"
VERDICT_PARTIAL = "goalserve_partial_tune_once"
VERDICT_FAIL = "goalserve_fail_market_only_or_sportradar"
VERDICT_NO_WINDOW = "insufficient_live_window"
VERDICT_NO_KEY = "no_key"

RECOMMENDATIONS = {
    VERDICT_PASS: (
        "adopt Goalserve as the live-state tape provider (API-Tennis remains "
        "the mapping catalog). Do NOT build models yet — next milestone is "
        "TENNIS-TAPE-GOALSERVE-001 (wire the feed into tape captures) or "
        "TENNIS-MICROSTRUCTURE-001, each explicitly accepted"
    ),
    VERDICT_PARTIAL: (
        "one tuning pass allowed (name/category mapping), then rerun during a "
        "denser live window before deciding"
    ),
    VERDICT_FAIL: (
        "stop pursuing live ITF state from aggregators: proceed with "
        "market-only tape research, or the Sportradar main-tour/Challenger "
        "path — and do not keep pushing API-Tennis for live state"
    ),
    VERDICT_NO_WINDOW: (
        "no live tennis candidates at validation time — rerun during an "
        "active ITF/Challenger window"
    ),
    VERDICT_NO_KEY: (
        "no Goalserve key configured — nothing was fetched; supply the "
        "host-only key and rerun"
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_date(raw: str | None) -> str | None:
    """Goalserve dates arrive as dd.mm.yyyy — normalize to yyyy-mm-dd."""
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _listify(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def normalize_goalserve_live(payload: dict | None) -> list[dict]:
    """Normalize a Goalserve tennis live feed into the fixture shape the
    existing tape linker consumes, carrying Goalserve's live-state fields.
    Matches missing either player are dropped (honest), never padded."""
    if not isinstance(payload, dict):
        return []
    scores = payload.get("scores") or {}
    out = []
    for category in _listify(scores.get("category")):
        if not isinstance(category, dict):
            continue
        cat_name = category.get("@name") or category.get("name") or ""
        for match in _listify(category.get("match")):
            if not isinstance(match, dict):
                continue
            players = _listify(match.get("player"))
            if len(players) != 2:
                continue
            name_a = players[0].get("@name") or players[0].get("name")
            name_b = players[1].get("@name") or players[1].get("name")
            if not name_a or not name_b:
                continue
            status = str(match.get("@status") or match.get("status") or "")
            sets = []
            for i in range(1, 6):
                sa, sb = players[0].get(f"s{i}"), players[1].get(f"s{i}")
                if sa not in (None, "") or sb not in (None, ""):
                    sets.append({"set": i, "a": sa, "b": sb})
            serve = None
            for idx, p in enumerate(players):
                if str(p.get("@serve") or p.get("serve") or "") in ("True", "true", "1"):
                    serve = "first" if idx == 0 else "second"
            in_play = bool(
                status and status.lower() not in
                ("finished", "cancelled", "canceled", "postponed", "walkover",
                 "retired", "not started")
                and not status.replace(":", "").isdigit()   # "14:30" = scheduled
            )
            out.append({
                # linker-compatible fixture shape
                "event_key": str(match.get("@id") or match.get("id") or ""),
                "event_date": _norm_date(match.get("@date") or match.get("date")),
                "event_first_player": name_a,
                "event_second_player": name_b,
                "event_status": status,
                "event_type_type": cat_name,
                # goalserve live-state fields
                "gs_sets": sets,
                "gs_game_score": (
                    f'{players[0].get("game_score") or players[0].get("@game_score") or ""}'
                    f'-{players[1].get("game_score") or players[1].get("@game_score") or ""}'
                ).strip("-") or None,
                "gs_point_score": match.get("@points") or match.get("points") or None,
                "gs_serve": serve,
                "gs_in_play": in_play,
            })
    return out


def live_state_signature(fixture: dict) -> tuple:
    return (
        fixture.get("event_status"),
        str(fixture.get("gs_sets")),
        fixture.get("gs_game_score"),
        fixture.get("gs_point_score"),
        fixture.get("gs_serve"),
    )


class GoalserveTennisClient:
    """Thin read-only Goalserve live-feed client. The key lives in the URL
    path, so the URL is NEVER logged/echoed; failures return None and are
    reported by exception type only."""

    source_name = "goalserve.com"

    def __init__(self, api_key: str = "", timeout: float = 15.0):
        self._api_key = api_key or ""
        self.timeout = timeout
        self.last_error: str | None = None

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    @property
    def display_url(self) -> str:
        return DISPLAY_URL

    async def fetch_live(self) -> dict | None:
        if not self.has_key:
            logger.warning("goalserve validation: no key — no request made")
            return None
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{GOALSERVE_BASE}/{self._api_key}/{LIVE_PATH}",
                    params={"json": "1"},
                )
                response.raise_for_status()
                return response.json()
        except Exception as exc:   # never raise; never echo the URL
            self.last_error = type(exc).__name__
            logger.warning("goalserve fetch failed: %s", type(exc).__name__)
            return None


class GoalserveValidationService:
    """Bounded repeated-probe validation. Persists nothing."""

    def __init__(self, client: GoalserveTennisClient | None = None,
                 settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = client or GoalserveTennisClient(
            api_key=self.settings.goalserve_tennis_api_key
        )

    def _candidates(self, session: Session, hours: int = 24) -> list[str]:
        universe = discover_tennis_universe(session, hours=hours)
        return sorted(
            m.ticker for m in universe.active
            if classify_tennis_market(m.ticker) == "match_winner"
            and parse_tennis_ticker(m.ticker) is not None
        )

    async def validate(
        self, session: Session, probes: int = 2, interval_sec: int = 20,
        top: int = 10,
    ) -> dict:
        probes = max(1, min(probes, MAX_PROBES, MAX_CALLS))
        interval_sec = max(5, min(interval_sec, 120))
        candidates = self._candidates(session)
        report = {
            "note": GOALSERVE_NOTE,
            "provider_tested": "goalserve.com tennis live feed",
            "display_url": self.client.display_url,
            "generated_at": _now().isoformat(),
            "live_candidates": len(candidates),
            "probes_planned": probes,
            "calls_made": 0,
            "fetch_errors": [],
            "live_rows_per_probe": [],
            "in_play_rows_per_probe": [],
            "state_changes": 0,
            "live_state_fields": {"sets": 0, "game_score": 0,
                                  "point_score": 0, "serve": 0},
            "matched_candidates": 0,
            "matched_examples": [],
            "miss_examples": [],
            "state_change_examples": [],
            "api_tennis_baseline": (
                "REST get_livescore 0 rows x 25+ probes; WS 0 frames in 180s "
                "decisive in-play probe; fixtures frozen while 24 Kalshi books "
                "moved (2026-07-10)"
            ),
        }
        if not self.client.has_key:
            report["verdict"] = VERDICT_NO_KEY
            report["recommendation"] = RECOMMENDATIONS[VERDICT_NO_KEY]
            return report

        seen_states: dict[str, tuple] = {}
        latest_rows: dict[str, dict] = {}
        for i in range(probes):
            payload = await self.client.fetch_live()
            report["calls_made"] += 1
            if payload is None:
                report["fetch_errors"].append(
                    self.client.last_error or "unknown"
                )
                report["live_rows_per_probe"].append(None)
                report["in_play_rows_per_probe"].append(None)
            else:
                rows = normalize_goalserve_live(payload)
                report["live_rows_per_probe"].append(len(rows))
                report["in_play_rows_per_probe"].append(
                    sum(1 for r in rows if r["gs_in_play"])
                )
                for row in rows:
                    key = row["event_key"] or f'{row["event_first_player"]}|{row["event_second_player"]}'
                    signature = live_state_signature(row)
                    if key in seen_states and seen_states[key] != signature:
                        report["state_changes"] += 1
                        if len(report["state_change_examples"]) < top:
                            report["state_change_examples"].append({
                                "players": f'{row["event_first_player"]} vs {row["event_second_player"]}',
                                "status": row["event_status"],
                                "sets": row["gs_sets"],
                                "point_score": row["gs_point_score"],
                            })
                    seen_states[key] = signature
                    latest_rows[key] = row
            if i < probes - 1:
                await asyncio.sleep(interval_sec)

        for row in latest_rows.values():
            if row["gs_sets"]:
                report["live_state_fields"]["sets"] += 1
            if row["gs_game_score"]:
                report["live_state_fields"]["game_score"] += 1
            if row["gs_point_score"]:
                report["live_state_fields"]["point_score"] += 1
            if row["gs_serve"]:
                report["live_state_fields"]["serve"] += 1

        # link latest provider rows to Kalshi candidates via the tape linker
        fixtures_by_date: dict[str, list] = {}
        for row in latest_rows.values():
            fixtures_by_date.setdefault(row["event_date"] or "unknown", []).append(row)
        matched, misses = [], []
        for ticker in candidates:
            outcome = link_candidate(ticker, fixtures_by_date)
            if outcome.label == LINK_SOURCE_BACKED:
                fx = outcome.fixture or {}
                matched.append({
                    "ticker": ticker,
                    "status": fx.get("event_status"),
                    "sets": fx.get("gs_sets"),
                    "in_play": fx.get("gs_in_play"),
                })
            else:
                misses.append({"ticker": ticker, "label": outcome.label})
        report["matched_candidates"] = len(matched)
        report["matched_examples"] = matched[:top]
        report["miss_examples"] = misses[:top]

        # verdict
        usable_rows = any(r for r in report["live_rows_per_probe"] if r)
        in_play_seen = any(r for r in report["in_play_rows_per_probe"] if r)
        state_ok = report["state_changes"] > 0 or (probes == 1 and in_play_seen)
        if not candidates:
            verdict = VERDICT_NO_WINDOW
        elif not usable_rows:
            verdict = VERDICT_FAIL
        elif report["matched_candidates"] > 0 and in_play_seen and state_ok:
            verdict = VERDICT_PASS
        elif report["matched_candidates"] > 0 or in_play_seen:
            verdict = VERDICT_PARTIAL
        else:
            verdict = VERDICT_FAIL
        report["verdict"] = verdict
        report["recommendation"] = RECOMMENDATIONS[verdict]
        return report
