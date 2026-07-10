"""TENNIS-LIVE-FEED-002 — bounded API-Tennis WebSocket live-feed VALIDATION.

The repeated tape sessions proved the market half (Kalshi books moved 24
probability points in-play) and disproved the REST score half (get_livescore
returned 0 rows in 25 probes; fixture state stayed frozen). This module tests
the provider's remaining live surface — the documented WebSocket
(`wss://wss.api-tennis.com/live?APIkey=...`, fixture-shaped events per their
docs) — before any provider switch.

Strictly bounded, read-only validation:
- Connects ONLY when `TENNIS_PROVIDER_API_KEY` is present; the key is never
  printed, never in display URLs, and connection errors are reported by type
  name only (never the connect URI).
- Runs for a fixed duration and then stops; no reconnect loop, no timer.
- Correlates emitted events to current Kalshi tennis candidates through the
  EXISTING tape linker (same ±1-day, both-player-code rules).
- Takes a small REST snapshot (get_livescore + fixtures) for side-by-side
  comparison in the same window.
- Persists NOTHING; output is a validation report with an honest verdict:
  api_tennis_ws_pass / api_tennis_ws_partial /
  api_tennis_ws_fail_goalserve_next / insufficient_live_window / no_key.

No probability models, Markov models, EV, paper trading, recommendations,
sizing, orders, wallets, signing, swaps, execution, or autonomy — and no
forecast/gate/promotion/MarketOps/EDGE-AUTO behavior change.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.services.tennis_live_source import classify_tennis_market
from app.services.tennis_research import get_tennis_fetcher, parse_tennis_ticker
from app.services.tennis_tape import LINK_SOURCE_BACKED, link_candidate
from app.services.tennis_watcher import discover_tennis_universe

logger = logging.getLogger(__name__)

LIVEFEED_NOTE = (
    "Bounded read-only WebSocket live-feed validation: does the provider emit "
    "usable live ITF/Challenger state? Connects only with an explicit key "
    "(never printed), runs for a fixed duration, persists nothing. Coverage "
    "measurement only — not a model, not EV, not trading, never advice."
)

WS_URL = "wss://wss.api-tennis.com/live"
MAX_DURATION_SEC = 300          # hard bound regardless of the flag value
REST_COMPARISON_CALLS = 3       # livescore + up to 2 fixture dates

VERDICT_PASS = "api_tennis_ws_pass"
VERDICT_PARTIAL = "api_tennis_ws_partial"
VERDICT_FAIL = "api_tennis_ws_fail_goalserve_next"
VERDICT_NO_WINDOW = "insufficient_live_window"
VERDICT_NO_KEY = "no_key"

RECOMMENDATIONS = {
    VERDICT_PASS: (
        "keep API-Tennis: the WebSocket delivers live state — wire it into a "
        "future tape capture milestone (explicitly accepted) and re-measure lag"
    ),
    VERDICT_PARTIAL: (
        "keep API-Tennis provisionally and tune the linker/subscription — "
        "events arrived but did not fully align with Kalshi candidates; rerun "
        "the probe during a denser live window before deciding"
    ),
    VERDICT_FAIL: (
        "test Goalserve next (30-day trial; explicit signup + key required): "
        "the WebSocket emitted no usable live state while Kalshi candidates "
        "were active — same falsifiable test, same fields, bounded calls, no "
        "persistence unless accepted"
    ),
    VERDICT_NO_WINDOW: (
        "no live tennis candidates at probe time — rerun during an active "
        "ITF/Challenger window before drawing any provider conclusion"
    ),
    VERDICT_NO_KEY: (
        "no provider key configured — nothing was connected; supply the "
        "host-only key and rerun"
    ),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FeedEvent:
    """One normalized WebSocket emission (fixture-shaped per provider docs)."""

    event_key: str
    event_date: str | None
    raw: dict
    received_at: float

    @property
    def state_signature(self) -> tuple:
        return (
            self.raw.get("event_status"),
            json.dumps(self.raw.get("scores") or [], sort_keys=True),
            len(self.raw.get("pointbypoint") or []),
            self.raw.get("event_serve"),
        )


def normalize_ws_message(message: str | bytes) -> list[dict]:
    """Parse one WS frame into fixture-shaped dicts. Providers emit either a
    single object or a list; anything unparseable is dropped (counted by the
    caller), never guessed."""
    try:
        payload = json.loads(message)
    except (ValueError, TypeError):
        return []
    if isinstance(payload, dict):
        # some feeds wrap in {"result": ...}
        inner = payload.get("result", payload)
        if isinstance(inner, list):
            return [x for x in inner if isinstance(x, dict)]
        return [inner] if isinstance(inner, dict) else []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


async def _real_stream(key: str, duration_sec: float):
    """Default stream factory: one bounded WebSocket connection. Yields raw
    frames until the deadline; never reconnects; never raises the connect URI
    (which embeds the key) upward."""
    import websockets

    deadline = time.monotonic() + duration_sec
    async with websockets.connect(
        f"{WS_URL}?APIkey={key}", open_timeout=15
    ) as ws:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                yield await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return


class TennisLiveFeedProbe:
    """Bounded WS probe + REST comparison. Persists nothing."""

    def __init__(
        self,
        settings: Settings | None = None,
        stream_factory=None,
        rest_fetcher=None,
    ):
        self.settings = settings or get_settings()
        self._stream_factory = stream_factory or _real_stream
        self._rest_fetcher = rest_fetcher

    def _candidates(self, session: Session, hours: int = 24):
        universe = discover_tennis_universe(session, hours=hours)
        out = []
        for m in universe.active:
            if classify_tennis_market(m.ticker) != "match_winner":
                continue
            if parse_tennis_ticker(m.ticker) is not None:
                out.append(m.ticker)
        return sorted(out)

    async def probe(
        self, session: Session, duration_sec: int = 60, top: int = 10,
    ) -> dict:
        duration_sec = max(5, min(duration_sec, MAX_DURATION_SEC))
        key = self.settings.tennis_provider_api_key
        candidates = self._candidates(session)
        report = {
            "note": LIVEFEED_NOTE,
            "provider_tested": "api-tennis.com websocket",
            "ws_display_url": WS_URL,          # key intentionally absent
            "generated_at": _now().isoformat(),
            "duration_sec": duration_sec,
            "live_candidates": len(candidates),
            "ws_frames": 0,
            "ws_events": 0,
            "ws_unparseable_frames": 0,
            "distinct_matches": 0,
            "state_changes": 0,
            "matched_candidates": 0,
            "matched_examples": [],
            "state_change_examples": [],
            "connection_error": None,
            "rest_comparison": None,
        }
        if not key:
            report["verdict"] = VERDICT_NO_KEY
            report["recommendation"] = RECOMMENDATIONS[VERDICT_NO_KEY]
            return report

        # --- WS collection (bounded) ---------------------------------------
        events: dict[str, list[FeedEvent]] = {}
        try:
            async for frame in self._stream_factory(key, duration_sec):
                report["ws_frames"] += 1
                parsed = normalize_ws_message(frame)
                if not parsed:
                    report["ws_unparseable_frames"] += 1
                    continue
                for raw in parsed:
                    ekey = str(raw.get("event_key") or "")
                    if not ekey:
                        report["ws_unparseable_frames"] += 1
                        continue
                    report["ws_events"] += 1
                    events.setdefault(ekey, []).append(FeedEvent(
                        event_key=ekey,
                        event_date=raw.get("event_date"),
                        raw=raw,
                        received_at=time.monotonic(),
                    ))
        except Exception as exc:
            # never surface the connect URI (embeds the key) — type name only
            report["connection_error"] = type(exc).__name__
            logger.warning("livefeed probe connection error: %s", type(exc).__name__)

        report["distinct_matches"] = len(events)
        for versions in events.values():
            signatures = {v.state_signature for v in versions}
            if len(signatures) > 1:
                report["state_changes"] += len(signatures) - 1
        change_examples = [
            {
                "players": f'{v[0].raw.get("event_first_player")} vs '
                           f'{v[0].raw.get("event_second_player")}',
                "versions": len(v),
                "first_status": v[0].raw.get("event_status"),
                "last_status": v[-1].raw.get("event_status"),
                "type": v[0].raw.get("event_type_type"),
            }
            for v in (sorted(vs, key=lambda e: e.received_at) for vs in events.values())
            if len({e.state_signature for e in v}) > 1
        ]
        report["state_change_examples"] = change_examples[:top]

        # --- link WS matches to Kalshi candidates ---------------------------
        fixtures_by_date: dict[str, list] = {}
        for versions in events.values():
            latest = max(versions, key=lambda e: e.received_at)
            date = latest.event_date or "unknown"
            fixtures_by_date.setdefault(date, []).append(latest.raw)
        matched = []
        for ticker in candidates:
            outcome = link_candidate(ticker, fixtures_by_date)
            if outcome.label == LINK_SOURCE_BACKED:
                matched.append({
                    "ticker": ticker,
                    "status": (outcome.fixture or {}).get("event_status"),
                })
        report["matched_candidates"] = len(matched)
        report["matched_examples"] = matched[:top]

        # --- REST comparison (bounded) ---------------------------------------
        report["rest_comparison"] = await self._rest_snapshot(candidates)

        # --- verdict -----------------------------------------------------------
        if not candidates:
            verdict = VERDICT_NO_WINDOW
        elif report["ws_events"] == 0:
            verdict = VERDICT_FAIL
        elif report["matched_candidates"] > 0 and report["state_changes"] > 0:
            verdict = VERDICT_PASS
        else:
            verdict = VERDICT_PARTIAL
        report["verdict"] = verdict
        report["recommendation"] = RECOMMENDATIONS[verdict]
        return report

    async def _rest_snapshot(self, candidates: list) -> dict:
        """Small REST snapshot for side-by-side comparison: one get_livescore
        + up to two fixture dates. Bounded; counts only."""
        fetcher = self._rest_fetcher
        if fetcher is None:
            fetcher = get_tennis_fetcher(self.settings)
        if fetcher is None or not getattr(fetcher, "has_key", False):
            return {"available": False, "reason": "rest fetcher not configured"}
        live_payload = await fetcher._get({"method": "get_livescore"})
        live_rows = len((live_payload or {}).get("result") or [])
        dates = []
        for t in candidates:
            ctx = parse_tennis_ticker(t)
            if ctx and ctx.date not in dates:
                dates.append(ctx.date)
        fixtures_by_date = {}
        for d in dates[: REST_COMPARISON_CALLS - 1]:
            payload = await fetcher._get({
                "method": "get_fixtures", "date_start": d, "date_stop": d,
            })
            fixtures_by_date[d] = (payload or {}).get("result") or []
        rest_matched = sum(
            1 for t in candidates
            if link_candidate(t, fixtures_by_date).label == LINK_SOURCE_BACKED
        )
        return {
            "available": True,
            "rest_livescore_rows": live_rows,
            "rest_fixture_dates": {d: len(v) for d, v in fixtures_by_date.items()},
            "rest_source_backed": rest_matched,
        }
