"""LIVE-MARKET-001 — read-only live market/state observation foundation.

Foundation for future in-game probability-market RESEARCH: what does our view
of a live market actually look like right now, how fresh is it, how volatile
is it, and what state information (scores) do we actually have? Everything is
computed on demand from rows that already exist — markets, price ticks, and
persisted research packets. Nothing new is persisted; no external call is
made; no migration is added.

Tennis scaffold (match winner only): score state is extracted from persisted
TENNIS-001 research-packet facts when a source_backed tennis packet exists for
the market; otherwise the state is `template_only` with an explicit
`provider_gap` — the repo has no validated live tennis score feed
(TENNIS_RESEARCH_PROVIDER defaults to "template"; the ESPN payload mapping is
unvalidated), and this module NEVER fabricates state or fetches anything.

Latency and volatility outputs are DIAGNOSTIC LABELS ONLY: `volatile_state` /
`calm_state` describe measured quote movement, never a signal to act. This
module carries no EV, no paper trading, no recommendations, no sizing, no
orders, no wallets/keys/signing/swaps/execution, and no autonomy, and it
changes no MarketOps/EDGE-AUTO/forecast/gate/flag behavior.
"""

import ast
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Market, MarketPriceTick, MarketResearchPacket
from app.services.db_growth import domain_for_ticker
from app.services.edge_followthrough import _aware, _mean

logger = logging.getLogger(__name__)

LIVE_STATE_NOTE = (
    "Read-only live-state OBSERVATION. Everything is computed from persisted "
    "markets, ticks, and research packets — no external call, nothing "
    "persisted, nothing fabricated. volatile_state/calm_state are diagnostic "
    "labels about measured quote movement, never signals and never advice. "
    "Not EV, not a recommendation, not trading; no sizing, orders, wallets, "
    "keys, swaps, signing, or execution."
)

# freshness / latency thresholds (seconds)
MARKET_FRESH_S = 300          # quotes younger than 5m = fresh
MARKET_STALE_S = 900          # older than 15m = stale_market_quotes
SCORE_FRESH_S = 900           # score info younger than 15m = fresh

# volatility thresholds (probability points on the 0-1 midpoint scale)
VOLATILE_MOVE_1M = 0.02
VOLATILE_MOVE_5M = 0.03
VOLATILE_MOVE_10M = 0.05
VOLATILE_MID_RANGE_10M = 0.05
VOLATILE_SPREAD_WIDEN_C = 3   # cents

STATUS_STATE_BACKED = "state_backed_live"
STATUS_MARKET_ONLY = "observable_market_only"
STATUS_STALE = "stale_market_quotes"
STATUS_INSUFFICIENT = "insufficient_live_data"

VOLATILE = "volatile_state"
CALM = "calm_state"
INSUFFICIENT = "insufficient_live_data"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- pure diagnostics ---------------------------------------------------------------


def quote_quality(
    bid: int | None, ask: int | None, spread: int | None, liquidity: int | None
) -> str:
    """missing_quotes / wide / moderate / tight — from the latest book."""
    if bid is None or ask is None:
        return "missing_quotes"
    s = spread if spread is not None else ask - bid
    if s >= 5:
        return "wide"
    if s >= 3:
        return "moderate"
    return "tight"


@dataclass
class TickPoint:
    at: datetime
    mid: float | None
    spread: int | None
    liquidity: int | None


def window_move(points: list[TickPoint], now: datetime, minutes: int) -> float | None:
    """Midpoint move (last - first) over the trailing window; None when fewer
    than two mid-bearing ticks fall inside it — never guessed."""
    start = now - timedelta(minutes=minutes)
    inside = [p for p in points if p.at >= start and p.mid is not None]
    if len(inside) < 2:
        return None
    return round(inside[-1].mid - inside[0].mid, 4)


def mid_range(points: list[TickPoint], now: datetime, minutes: int) -> float | None:
    start = now - timedelta(minutes=minutes)
    mids = [p.mid for p in points if p.at >= start and p.mid is not None]
    if len(mids) < 2:
        return None
    return round(max(mids) - min(mids), 4)


def spread_delta(points: list[TickPoint], now: datetime, minutes: int) -> int | None:
    start = now - timedelta(minutes=minutes)
    spreads = [p.spread for p in points if p.at >= start and p.spread is not None]
    if len(spreads) < 2:
        return None
    return spreads[-1] - spreads[0]


def liquidity_delta(points: list[TickPoint], now: datetime, minutes: int) -> int | None:
    start = now - timedelta(minutes=minutes)
    liqs = [p.liquidity for p in points if p.at >= start and p.liquidity is not None]
    if len(liqs) < 2:
        return None
    return liqs[-1] - liqs[0]


def quote_instability(points: list[TickPoint], now: datetime, minutes: int = 10) -> float | None:
    """Share of consecutive tick pairs whose midpoint CHANGED over the window
    — 0.0 = frozen book, 1.0 = every observation moved. None under two ticks."""
    start = now - timedelta(minutes=minutes)
    mids = [p.mid for p in points if p.at >= start and p.mid is not None]
    if len(mids) < 2:
        return None
    changes = sum(1 for a, b in zip(mids, mids[1:]) if abs(b - a) > 1e-9)
    return round(changes / (len(mids) - 1), 4)


def classify_volatility(
    move_1m: float | None, move_5m: float | None, move_10m: float | None,
    range_10m: float | None, spread_d: int | None,
) -> tuple[str, str]:
    """volatile_state / calm_state / insufficient_live_data + reason.
    Diagnostic labels about measured movement — never a signal."""
    reasons = []
    if move_1m is not None and abs(move_1m) >= VOLATILE_MOVE_1M:
        reasons.append(f"|move_1m|={abs(move_1m)} >= {VOLATILE_MOVE_1M}")
    if move_5m is not None and abs(move_5m) >= VOLATILE_MOVE_5M:
        reasons.append(f"|move_5m|={abs(move_5m)} >= {VOLATILE_MOVE_5M}")
    if move_10m is not None and abs(move_10m) >= VOLATILE_MOVE_10M:
        reasons.append(f"|move_10m|={abs(move_10m)} >= {VOLATILE_MOVE_10M}")
    if range_10m is not None and range_10m >= VOLATILE_MID_RANGE_10M:
        reasons.append(f"mid_range_10m={range_10m} >= {VOLATILE_MID_RANGE_10M}")
    if spread_d is not None and spread_d >= VOLATILE_SPREAD_WIDEN_C:
        reasons.append(f"spread widened +{spread_d}c")
    if reasons:
        return VOLATILE, "; ".join(reasons)
    if move_5m is None and move_10m is None:
        return INSUFFICIENT, "fewer than two ticks in the 5m and 10m windows"
    return CALM, "all measured moves below volatility thresholds"


# --- tennis scaffold (match winner only) ---------------------------------------------


PLAYER_PATTERNS = (
    re.compile(r"^will\s+(.+?)\s+(?:beat|defeat)\s+(.+?)[?\s]*$", re.IGNORECASE),
    re.compile(r"^(.+?)\s+vs\.?\s+(.+?)(?:\s+winner|\s+match)?[?\s]*$", re.IGNORECASE),
    re.compile(r"^(.+?)\s+to\s+(?:beat|defeat|win against)\s+(.+?)[?\s]*$", re.IGNORECASE),
)

SETS_RE = re.compile(r"sets\s+(\d+)-(\d+)")
GAMES_RE = re.compile(r"games\s+(\[[^\]]*\])\s+vs\s+(\[[^\]]*\])")
SERVER_RE = re.compile(r"(?:serving|server)[:\s]+([A-Za-z .'-]+)", re.IGNORECASE)


def parse_players(title: str | None) -> tuple[str | None, str | None]:
    """Best-effort player extraction from a persisted market title. None/None
    when the title does not match a known match-winner shape — never guessed."""
    if not title:
        return None, None
    for pattern in PLAYER_PATTERNS:
        m = pattern.match(title.strip())
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None, None


def extract_tennis_state(key_facts: list | None) -> dict:
    """Parse set/game/server/status from persisted TENNIS-001 research-packet
    fact strings (e.g. 'Match state: A vs B — sets 1-0 (games [6, 2] vs
    [4, 2]); In Progress'). Absent facts stay None and are listed in
    missing_info — nothing is fabricated."""
    state: dict = {
        "set_score": None, "game_score": None, "point_score": None,
        "server": None, "match_status": None,
    }
    for entry in key_facts or []:
        text = entry.get("fact", "") if isinstance(entry, dict) else str(entry)
        m = SETS_RE.search(text)
        if m and state["set_score"] is None:
            state["set_score"] = f"{m.group(1)}-{m.group(2)}"
        g = GAMES_RE.search(text)
        if g and state["game_score"] is None:
            try:
                a, b = ast.literal_eval(g.group(1)), ast.literal_eval(g.group(2))
                if a and b:
                    state["game_score"] = f"{a[-1]}-{b[-1]}"
            except (ValueError, SyntaxError):
                pass
        s = SERVER_RE.search(text)
        if s and state["server"] is None:
            state["server"] = s.group(1).strip()
        if "match state:" in text.lower() and ";" in text and state["match_status"] is None:
            state["match_status"] = text.rsplit(";", 1)[-1].strip()
    return state


# --- report service -------------------------------------------------------------------


@dataclass
class LiveObservation:
    """One market's live observation. Measurements and labels only."""

    market_ticker: str
    domain: str
    market_type: str
    participants: tuple[str | None, str | None]
    market_status: str | None
    market_mid: float | None = None
    bid: int | None = None
    ask: int | None = None
    spread: int | None = None
    liquidity: int | None = None
    last_market_update_at: datetime | None = None
    last_score_update_at: datetime | None = None
    market_freshness_s: float | None = None
    score_freshness_s: float | None = None
    score_to_market_lag_s: float | None = None
    market_moved_since_last_score: bool | None = None
    quote_quality: str = "missing_quotes"
    state_quality: str = "market_only"
    live_observation_status: str = STATUS_INSUFFICIENT
    volatility_label: str = INSUFFICIENT
    volatility_reason: str = ""
    moves: dict = field(default_factory=dict)
    spread_delta_10m: int | None = None
    liquidity_delta_10m: int | None = None
    quote_instability_10m: float | None = None
    tennis: dict | None = None
    warnings: list = field(default_factory=list)


MARKET_TYPE_MARKERS = (
    ("TOTAL", "total"), ("SPREAD", "spread"), ("GAME", "winner"),
    ("MATCH", "winner"), ("WINNER", "winner"),
)


def _market_type_for(ticker: str) -> str:
    series = (ticker or "").split("-")[0].upper()
    for marker, mt in MARKET_TYPE_MARKERS:
        if marker in series:
            return mt
    return "unknown"


class LiveMarketStateReportService:
    """Builds the live-state observation report. Read-only; persists nothing;
    fetches nothing."""

    def build(
        self, session: Session, domain: str = "sports_tennis",
        top: int = 10, hours: int = 6,
    ) -> dict:
        now = _now()
        start = now - timedelta(hours=hours)
        tickers = [
            t for (t,) in session.execute(
                select(MarketPriceTick.market_ticker)
                .where(MarketPriceTick.observed_at >= start)
                .distinct()
            ).all()
            if domain_for_ticker(t) == domain
        ]
        markets = {
            m.ticker: m
            for m in session.execute(
                select(Market).where(Market.ticker.in_(tickers))
            ).scalars().all()
        } if tickers else {}

        observations: list[LiveObservation] = []
        for ticker in tickers:
            obs = self._observe(session, ticker, domain, markets.get(ticker), now)
            observations.append(obs)
        observations.sort(
            key=lambda o: o.last_market_update_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        observations = observations[: max(top, 0)]

        state_backed = sum(
            1 for o in observations if o.live_observation_status == STATUS_STATE_BACKED
        )
        template_only = sum(
            1 for o in observations
            if o.tennis is not None and o.tennis.get("source") == "template_only"
        )
        stale = [o.market_ticker for o in observations
                 if o.live_observation_status == STATUS_STALE]
        volatile = sorted(
            (o for o in observations if o.volatility_label == VOLATILE),
            key=lambda o: -abs(o.moves.get("5m") or o.moves.get("10m") or 0),
        )
        provider_gaps = []
        if domain == "sports_tennis":
            if not any(o.tennis and o.tennis.get("source") == "source_backed"
                       for o in observations):
                provider_gaps.append(
                    "provider_gap: no validated live tennis score source — "
                    "TENNIS_RESEARCH_PROVIDER defaults to 'template' and the "
                    "TENNIS-001 ESPN payload mapping is unvalidated; score "
                    "fields are template_only, never fabricated"
                )
        warnings = []
        if not observations:
            warnings.append(
                f"insufficient_live_data: no {domain} markets have ticks in the "
                f"last {hours}h"
            )
        elif all(o.live_observation_status == STATUS_STALE for o in observations):
            warnings.append(
                f"stale_provider: every observed {domain} market's latest quote "
                f"is older than {MARKET_STALE_S}s"
            )

        return {
            "note": LIVE_STATE_NOTE,
            "domain": domain,
            "window_hours": hours,
            "generated_at": now.isoformat(),
            "live_candidates": len(observations),
            "state_backed_count": state_backed,
            "template_only_count": template_only,
            "quote_quality_mix": self._mix(observations, lambda o: o.quote_quality),
            "status_mix": self._mix(observations, lambda o: o.live_observation_status),
            "mean_market_freshness_s": _mean(
                [o.market_freshness_s for o in observations]
            ),
            "provider_gaps": provider_gaps,
            "warnings": warnings + stale_warnings(stale),
            "volatile_examples": [
                {
                    "ticker": o.market_ticker,
                    "move_5m": o.moves.get("5m"),
                    "move_10m": o.moves.get("10m"),
                    "reason": o.volatility_reason,
                }
                for o in volatile[:5]
            ],
            "observations": observations,
        }

    @staticmethod
    def _mix(observations: list[LiveObservation], key_fn) -> dict:
        out: dict[str, int] = {}
        for o in observations:
            k = key_fn(o)
            out[k] = out.get(k, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def _observe(
        self, session: Session, ticker: str, domain: str,
        market: Market | None, now: datetime,
    ) -> LiveObservation:
        obs = LiveObservation(
            market_ticker=ticker,
            domain=domain,
            market_type=_market_type_for(ticker),
            participants=parse_players(market.title if market else None),
            market_status=market.status if market else None,
        )
        ticks = session.execute(
            select(MarketPriceTick)
            .where(
                MarketPriceTick.market_ticker == ticker,
                MarketPriceTick.observed_at >= now - timedelta(minutes=30),
            )
            .order_by(MarketPriceTick.observed_at.asc(), MarketPriceTick.id.asc())
        ).scalars().all()
        points = [
            TickPoint(at=_aware(t.observed_at), mid=t.midpoint, spread=t.spread,
                      liquidity=t.liquidity_proxy)
            for t in ticks
        ]
        if ticks:
            last = ticks[-1]
            obs.market_mid = last.midpoint
            obs.bid, obs.ask, obs.spread = last.yes_bid, last.yes_ask, last.spread
            obs.liquidity = last.liquidity_proxy
            obs.last_market_update_at = _aware(last.observed_at)
            obs.market_freshness_s = round(
                (now - obs.last_market_update_at).total_seconds(), 1
            )
        obs.quote_quality = quote_quality(obs.bid, obs.ask, obs.spread, obs.liquidity)
        obs.moves = {
            f"{m}m": window_move(points, now, m) for m in (1, 5, 10)
        }
        obs.spread_delta_10m = spread_delta(points, now, 10)
        obs.liquidity_delta_10m = liquidity_delta(points, now, 10)
        obs.quote_instability_10m = quote_instability(points, now, 10)
        obs.volatility_label, obs.volatility_reason = classify_volatility(
            obs.moves.get("1m"), obs.moves.get("5m"), obs.moves.get("10m"),
            mid_range(points, now, 10), obs.spread_delta_10m,
        )

        if domain == "sports_tennis":
            obs.tennis = self._tennis_state(session, ticker, market, obs, now)

        # latency + status ladder
        if obs.last_market_update_at is None:
            obs.live_observation_status = STATUS_INSUFFICIENT
            obs.state_quality = "none"
            obs.warnings.append("insufficient_live_data: no ticks in 30m window")
        elif obs.market_freshness_s is not None and obs.market_freshness_s > MARKET_STALE_S:
            obs.live_observation_status = STATUS_STALE
            obs.state_quality = "market_only"
            obs.warnings.append(
                f"stale_provider: latest quote is {obs.market_freshness_s:.0f}s old"
            )
        elif (
            obs.last_score_update_at is not None
            and obs.score_freshness_s is not None
            and obs.score_freshness_s <= SCORE_FRESH_S
        ):
            obs.live_observation_status = STATUS_STATE_BACKED
            obs.state_quality = "score_backed_fresh"
        else:
            obs.live_observation_status = STATUS_MARKET_ONLY
            obs.state_quality = (
                "score_backed_stale" if obs.last_score_update_at else "market_only"
            )
        return obs

    def _tennis_state(
        self, session: Session, ticker: str, market: Market | None,
        obs: LiveObservation, now: datetime,
    ) -> dict:
        """Latest persisted tennis research state for the market — extracted,
        never fetched, never fabricated."""
        packet = session.execute(
            select(MarketResearchPacket)
            .where(
                MarketResearchPacket.market_ticker == ticker,
                MarketResearchPacket.domain == "sports_tennis",
            )
            .order_by(MarketResearchPacket.created_at.desc())
            .limit(1)
        ).scalars().first()
        player_a, player_b = obs.participants
        missing = []
        if player_a is None or player_b is None:
            missing.append("participants (title not in a known match-winner shape)")
        source_backed = bool(
            packet is not None
            and packet.collector_name not in ("template", "template_research")
            and packet.key_facts
        )
        state = extract_tennis_state(packet.key_facts if source_backed else None)
        for key in ("set_score", "game_score", "point_score", "server", "match_status"):
            if state[key] is None:
                missing.append(key)
        if source_backed:
            obs.last_score_update_at = _aware(packet.created_at)
            obs.score_freshness_s = round(
                (now - obs.last_score_update_at).total_seconds(), 1
            )
            if obs.last_market_update_at is not None:
                obs.score_to_market_lag_s = round(
                    (obs.last_market_update_at - obs.last_score_update_at).total_seconds(), 1
                )
                move_10m = obs.moves.get("10m")
                obs.market_moved_since_last_score = bool(
                    obs.score_freshness_s > 600
                    and move_10m is not None and abs(move_10m) >= VOLATILE_MOVE_5M
                )
        return {
            "player_a": player_a,
            "player_b": player_b,
            **state,
            "source": "source_backed" if source_backed else "template_only",
            "missing_info": missing,
            "provenance": {
                "collector": packet.collector_name if packet else None,
                "packet_created_at": (
                    _aware(packet.created_at).isoformat() if packet else None
                ),
                "note": (
                    "persisted TENNIS-001 research packet"
                    if source_backed else
                    "provider_gap: no source_backed tennis packet for this market"
                ),
            },
        }


def stale_warnings(stale_tickers: list[str]) -> list[str]:
    if not stale_tickers:
        return []
    return [
        f"stale_provider: {len(stale_tickers)} market(s) with quotes older than "
        f"{MARKET_STALE_S}s: {', '.join(stale_tickers[:5])}"
    ]
