"""TENNIS-LIVE-SOURCE-001 — read-only tennis provider/source VALIDATION.

LIVE-MARKET-001 showed sports_tennis at 0 live candidates with an honest
provider_gap. This module answers the follow-up question: *can the tennis
markets we have persisted be mapped to reliable source-backed live match
state at all?* It validates the mapping chain — ticker → players → tour/date
→ provider scoreboard event — using ONLY the existing TENNIS-001 scaffolds
(`parse_tennis_ticker`, `get_tennis_fetcher`, `_find_event`). Nothing new is
integrated and nothing is fabricated:

- With the default `TENNIS_RESEARCH_PROVIDER=template` there is NO fetcher and
  NO external call: every candidate reports `provider_gap` honestly.
- With a configured provider (e.g. `espn`, flag-gated elsewhere), scoreboards
  are fetched read-only, at most once per (tour, date), bounded, and each
  match-winner candidate either maps to an event (`source_backed`) or reports
  `provider_no_match`. Fetch failures surface as `stale_provider` warnings.

This is SOURCE VALIDATION ONLY: it measures coverage, mapping rates, and
freshness. It produces no probability updates, no EV, no recommendations, no
sizing, no orders, no wallets/keys/signing/swaps/execution, no autonomy, and
changes no MarketOps/EDGE-AUTO/forecast/gate/flag behavior. Persists nothing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, MarketPriceTick
from app.services.edge_followthrough import _aware, _rate
from app.services.tennis_research import (
    TennisDataFetcher,
    TennisMatchContext,
    _find_event,
    get_tennis_fetcher,
    parse_tennis_ticker,
)

logger = logging.getLogger(__name__)

TENNIS_SOURCE_NOTE = (
    "Read-only tennis provider/source validation. Persisted tennis markets are "
    "mapped against the existing TENNIS-001 provider scaffold only — with the "
    "default template provider nothing is fetched and every row is an honest "
    "provider_gap; with a configured provider, scoreboards are read-only and "
    "bounded. Coverage measurement only — no probability updates, not EV, "
    "never advice; no sizing, orders, wallets, keys, swaps, signing, or "
    "execution."
)

TENNIS_PREFIXES = ("KXATP", "KXWTA", "KXITF")
MAX_SCOREBOARD_FETCHES = 6      # hard bound on (tour, date) fetches per run

MAP_SOURCE_BACKED = "source_backed"
MAP_NO_MATCH = "provider_no_match"
MAP_GAP = "provider_gap"
MAP_UNPARSEABLE = "ticker_unparseable"
MAP_NOT_WINNER = "not_match_winner"


def classify_tennis_market(ticker: str) -> str:
    """match_winner / set_winner / prop / unknown from the ticker's series
    fragment. Honest `unknown` when the shape is unfamiliar."""
    upper = (ticker or "").upper()
    prefix = next((p for p in TENNIS_PREFIXES if upper.startswith(p)), None)
    if prefix is None:
        return "unknown"
    fragment = upper[len(prefix):].split("-", 1)[0]
    if "SET" in fragment:
        return "set_winner"
    # prop markers first: "TOTALGAMES" must not be caught by the "GAME" marker
    if any(m in fragment for m in ("TOTAL", "ACES", "TIEBREAK", "SPREAD")):
        return "prop"
    if any(m in fragment for m in ("MATCH", "WINNER", "WIN", "GAME")):
        return "match_winner"
    return "unknown"


@dataclass
class TennisSourceCandidate:
    """One persisted tennis market's mapping-validation result."""

    market_ticker: str
    title: str | None
    market_classification: str
    context_parsed: bool
    tour: str | None = None
    event_date: str | None = None
    player_a: str | None = None
    player_b: str | None = None
    players_mapped: bool = False
    mapping_status: str = MAP_GAP
    event_status: str | None = None
    fetched_at: str | None = None
    market_quote_age_s: float | None = None
    score_to_market_lag_s: float | None = None
    is_live_candidate: bool = False
    notes: list = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TennisLiveSourceReportService:
    """Builds the tennis source-validation report. Read-only; persists
    nothing; fetches nothing unless a provider is explicitly configured."""

    def __init__(self, fetcher: TennisDataFetcher | None = None, use_settings_fetcher: bool = True):
        # explicit fetcher wins (tests); otherwise the TENNIS-001 provider
        # selection applies (None for the default template provider)
        self._fetcher = fetcher if fetcher is not None else (
            get_tennis_fetcher() if use_settings_fetcher else None
        )

    async def build(self, session: Session, top: int = 10, hours: int = 24) -> dict:
        now = _now()
        markets = [
            m for m in session.execute(select(Market)).scalars().all()
            if (m.ticker or "").upper().startswith(TENNIS_PREFIXES)
        ]
        recent_cutoff = now - timedelta(hours=hours)
        scoreboards: dict[tuple[str, str], dict | None] = {}
        fetch_failures: list[str] = []
        candidates: list[TennisSourceCandidate] = []

        for m in markets:
            cand = TennisSourceCandidate(
                market_ticker=m.ticker,
                title=m.title or None,
                market_classification=classify_tennis_market(m.ticker),
                context_parsed=False,
            )
            cand.is_live_candidate = bool(
                m.last_seen_at and _aware(m.last_seen_at) >= recent_cutoff
                and (m.status or "").lower() in ("active", "open", "unknown", "")
            )
            last_tick_at = session.execute(
                select(MarketPriceTick.observed_at)
                .where(MarketPriceTick.market_ticker == m.ticker)
                .order_by(MarketPriceTick.observed_at.desc())
                .limit(1)
            ).scalar()
            if last_tick_at is not None:
                cand.market_quote_age_s = round(
                    (now - _aware(last_tick_at)).total_seconds(), 1
                )

            context = parse_tennis_ticker(m.ticker)
            if context is None:
                cand.mapping_status = MAP_UNPARSEABLE
                cand.notes.append("ticker does not match the known tennis shape")
                candidates.append(cand)
                continue
            cand.context_parsed = True
            cand.tour = context.tour
            cand.event_date = context.date
            cand.player_a, cand.player_b = context.player_a, context.player_b
            cand.players_mapped = bool(context.player_a and context.player_b)
            if not cand.players_mapped:
                cand.notes.append(
                    "matchup fragment does not split into two player codes"
                )

            if cand.market_classification != "match_winner":
                cand.mapping_status = MAP_NOT_WINNER
                cand.notes.append(
                    "only match-winner markets are provider-mappable in v1"
                )
                candidates.append(cand)
                continue

            if self._fetcher is None:
                cand.mapping_status = MAP_GAP
                cand.notes.append(
                    "no provider configured (TENNIS_RESEARCH_PROVIDER=template) "
                    "— no fetch attempted"
                )
                candidates.append(cand)
                continue

            cand.mapping_status = await self._map_via_provider(
                cand, context, scoreboards, fetch_failures, now
            )
            candidates.append(cand)

        match_winner = [c for c in candidates if c.market_classification == "match_winner"]
        mapped = [c for c in match_winner if c.mapping_status == MAP_SOURCE_BACKED]
        warnings = []
        if self._fetcher is None:
            warnings.append(
                "provider_gap: TENNIS_RESEARCH_PROVIDER=template — mapping was "
                "validated structurally (tickers/players/tours) but no live "
                "source was queried; configure a provider to test coverage"
            )
        for failure in sorted(set(fetch_failures)):
            warnings.append(f"stale_provider: scoreboard fetch failed for {failure}")
        if not markets:
            warnings.append("insufficient_data: no persisted sports_tennis markets")

        live = [c for c in candidates if c.is_live_candidate]
        examples = sorted(
            candidates,
            key=lambda c: (
                c.mapping_status != MAP_SOURCE_BACKED,
                not c.is_live_candidate,
                c.market_ticker,
            ),
        )[: max(top, 0)]
        return {
            "note": TENNIS_SOURCE_NOTE,
            "generated_at": now.isoformat(),
            "provider": getattr(self._fetcher, "source_name", None) or "template (none)",
            "window_hours": hours,
            "total_tennis_markets": len(markets),
            "live_candidates": len(live),
            "match_winner_candidates": len(match_winner),
            "classification_mix": self._mix(candidates, lambda c: c.market_classification),
            "mapping_status_mix": self._mix(candidates, lambda c: c.mapping_status),
            "provider_match_rate": _rate(len(mapped), len(match_winner)),
            "source_backed_count": len(mapped),
            "missing_player_mapping_count": sum(
                1 for c in candidates if c.context_parsed and not c.players_mapped
            ),
            "unparseable_ticker_count": sum(
                1 for c in candidates if not c.context_parsed
            ),
            "scoreboards_fetched": len([v for v in scoreboards.values() if v is not None]),
            "warnings": warnings,
            "examples": examples,
        }

    async def _map_via_provider(
        self,
        cand: TennisSourceCandidate,
        context: TennisMatchContext,
        scoreboards: dict,
        fetch_failures: list[str],
        now: datetime,
    ) -> str:
        key = (context.tour, context.date)
        if key not in scoreboards:
            if len(scoreboards) >= MAX_SCOREBOARD_FETCHES:
                cand.notes.append(
                    f"scoreboard fetch bound ({MAX_SCOREBOARD_FETCHES}) reached "
                    f"— not fetched this run"
                )
                return MAP_GAP
            scoreboards[key] = await self._fetcher.fetch_scoreboard(
                context.tour, context.date
            )
            if scoreboards[key] is None:
                fetch_failures.append(f"{context.tour}/{context.date}")
        scoreboard = scoreboards[key]
        if scoreboard is None:
            cand.notes.append("scoreboard unavailable for this tour/date")
            return MAP_GAP
        event = _find_event(scoreboard, context)
        if event is None:
            cand.notes.append(
                "no scoreboard event matched the ticker's players — provider "
                "does not cover this event (e.g. Challenger/ITF gap)"
            )
            return MAP_NO_MATCH
        status = (((event.get("status") or {}).get("type")) or {})
        cand.event_status = status.get("description") or status.get("state") or "unknown"
        cand.fetched_at = now.isoformat()
        if cand.market_quote_age_s is not None:
            # scoreboard was fetched at `now`, so the freshest possible score
            # view is now — the lag to our latest quote IS the quote age
            cand.score_to_market_lag_s = cand.market_quote_age_s
        return MAP_SOURCE_BACKED

    @staticmethod
    def _mix(candidates: list[TennisSourceCandidate], key_fn) -> dict:
        out: dict[str, int] = {}
        for c in candidates:
            k = key_fn(c)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))
