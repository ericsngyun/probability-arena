"""POLY-002: read-only Kalshi <-> Polymarket cross-venue OBSERVATION.

Identifies COMPARABLE markets across the two venues by deterministic semantic
matching (title/outcome/resolution normalization) and MEASURES observable
differences (midpoints on a 0..1 probability scale, spreads, liquidity proxies).

Hard boundary (docs/SAFETY_BOUNDARIES.md): this is observation and measurement
only. It does NOT compute EV, label arbitrage/"arb", identify trades, recommend
a side, size a position, place/cancel orders, paper trade, or touch
wallets/keys/swaps/signing/execution. A `match_label` is a semantic-comparability
verdict for human review; an `observed_difference` is a measured probability gap
between two venues' quotes — never a signal, a return, or an action. All inputs
are already-persisted rows (Kalshi markets/snapshots + POLY-001 polymarket
markets); no external call is made here.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CrossVenueMarketCandidate,
    CrossVenueObservationRun,
    Market,
    MarketSnapshot,
    PolymarketMarket,
)

logger = logging.getLogger(__name__)

# --- match labels (semantic comparability verdicts for human review) --------
LABEL_COMPARABLE = "comparable_market_candidate"
LABEL_UNRESOLVED = "unresolved_semantic_match"
LABEL_INCOMPATIBLE_OUTCOME = "incompatible_outcome"
LABEL_INCOMPATIBLE_RESOLUTION = "incompatible_resolution"
LABEL_LOW_CONFIDENCE = "low_confidence_match"
MATCH_LABELS = (
    LABEL_COMPARABLE, LABEL_LOW_CONFIDENCE, LABEL_UNRESOLVED,
    LABEL_INCOMPATIBLE_OUTCOME, LABEL_INCOMPATIBLE_RESOLUTION,
)

# thresholds (conservative + deterministic)
MIN_TITLE_SIM_FLOOR = 0.2      # below this there is no plausible comparable at all
HIGH_SIM = 0.45
LOW_SIM = 0.30
RESOLUTION_PROXIMATE_DAYS = 3
RESOLUTION_MAX_DAYS = 10       # beyond this the two markets resolve on different events

DISCLAIMER = (
    "Read-only cross-venue OBSERVATION. `match_label` is a semantic-comparability "
    "verdict for human review and `observed_difference` is a measured probability "
    "gap between the two venues' midpoints — NOT arbitrage, NOT EV, NOT a trade, "
    "NOT a side, NOT a size, NOT a recommendation, NOT an action. No dollars, "
    "profit, orders, wallets, keys, swaps, signing, or execution."
)

_STOPWORDS = frozenset({
    "will", "the", "a", "an", "to", "at", "on", "in", "of", "for", "be", "by",
    "and", "or", "is", "are", "this", "that", "who", "what", "which", "market",
    "vs", "versus", "v",
})
_OUTCOME_YES_NO = "yes_no"
_OUTCOME_WINNER = "winner"
_OUTCOME_OVER_UNDER = "over_under"
_OUTCOME_SPREAD = "spread"
_OUTCOME_ADVANCE = "advance"
_OUTCOME_CANDIDATE = "candidate_winner"
_OUTCOME_EVENT = "event_outcome"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- deterministic semantic normalizer --------------------------------------


def normalize_title(text: str | None) -> str:
    """Lowercase, strip punctuation, normalize vs/versus + whitespace. Pure."""
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"\bvs?\.?\b|\bversus\b", " vs ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def title_tokens(text: str | None) -> frozenset[str]:
    return frozenset(w for w in normalize_title(text).split() if w and w not in _STOPWORDS)


def normalize_outcome(text: str | None) -> str:
    """Map a market title/outcome to a canonical outcome TYPE (conservative)."""
    t = normalize_title(text)
    if not t:
        return _OUTCOME_EVENT
    if "advance" in t or "to advance" in t:
        return _OUTCOME_ADVANCE
    if "over" in t or "under" in t or re.search(r"\bo/?u\b", t) or "total" in t:
        return _OUTCOME_OVER_UNDER
    if "spread" in t or re.search(r"[+-]\s?\d", t):
        return _OUTCOME_SPREAD
    if any(w in t for w in ("election", "elected", "president", "nominee", "leader", "power")):
        return _OUTCOME_CANDIDATE
    if any(w in t for w in ("win", "winner", "champion")):
        return _OUTCOME_WINNER
    return _OUTCOME_YES_NO


# outcome types that describe the SAME kind of yes-probability question and can
# therefore be compared on a shared 0..1 midpoint scale
_YESISH = frozenset({_OUTCOME_YES_NO, _OUTCOME_WINNER, _OUTCOME_ADVANCE, _OUTCOME_CANDIDATE})


def outcomes_compatible(a: str, b: str) -> bool:
    if a == b:
        return True
    return a in _YESISH and b in _YESISH


def coarse_domain(*texts: str | None) -> str:
    """Coarse topic bucket used to gate matching. Conservative: unknown -> other."""
    blob = " ".join(normalize_title(t) for t in texts if t)
    if any(w in blob for w in (
        "world cup", "wimbledon", "tennis", "nba", "nfl", "mlb", "soccer",
        "football", "game", "match", "champion", "vs", "corners", "goals",
    )):
        return "sports"
    if any(w in blob for w in (
        "election", "president", "nominee", "leader", "power", "senate",
        "congress", "governor", "prime minister", "party",
    )):
        return "politics"
    if any(w in blob for w in ("bitcoin", "ethereum", "eth", "btc", "crypto", "solana")):
        return "crypto"
    if any(w in blob for w in ("gdp", "inflation", "cpi", "fed", "rate", "unemployment")):
        return "economics"
    return "other"


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


# --- venue view dataclasses -------------------------------------------------


@dataclass
class KalshiView:
    ticker: str
    event_ticker: str | None
    title: str
    domain: str
    outcome_type: str
    tokens: frozenset[str]
    resolution_time: datetime | None
    midpoint: float | None
    spread: float | None
    liquidity_proxy: float | None
    snapshot_age_seconds: float | None


@dataclass
class PolyView:
    market_id: str
    condition_id: str | None
    token_id: str | None
    question: str
    domain: str
    outcome_type: str
    tokens: frozenset[str]
    resolution_time: datetime | None
    midpoint: float | None
    spread: float | None
    liquidity_proxy: float | None


def _kalshi_midpoint(snap: MarketSnapshot) -> tuple[float | None, float | None]:
    """(midpoint, spread) on a 0..1 probability scale from cents. None when the
    quote is one-sided/absent (no fabricated price)."""
    if snap.yes_bid is not None and snap.yes_ask is not None:
        return round((snap.yes_bid + snap.yes_ask) / 200, 4), round((snap.yes_ask - snap.yes_bid) / 100, 4)
    if snap.last_price is not None:
        return round(snap.last_price / 100, 4), None
    return None, None


def _poly_midpoint(m: PolymarketMarket) -> tuple[float | None, float | None]:
    if m.best_bid is not None and m.best_ask is not None:
        return round((m.best_bid + m.best_ask) / 2, 4), (round(m.spread, 4) if m.spread is not None else None)
    prices = m.outcome_prices if isinstance(m.outcome_prices, list) else None
    if prices:
        try:
            return round(float(prices[0]), 4), (round(m.spread, 4) if m.spread is not None else None)
        except (TypeError, ValueError):
            pass
    return None, (round(m.spread, 4) if m.spread is not None else None)


class CrossVenueMatchingService:
    """Deterministic Kalshi<->Polymarket matcher + observer over persisted rows."""

    def _load_polymarket(self, session: Session, limit: int) -> list[PolyView]:
        rows = session.execute(
            select(PolymarketMarket).order_by(PolymarketMarket.id.desc())
        ).scalars().all()
        latest: dict[str, PolymarketMarket] = {}
        for m in rows:
            latest.setdefault(m.market_id, m)
        views: list[PolyView] = []
        for m in list(latest.values())[:limit]:
            mid, spread = _poly_midpoint(m)
            token_id = None
            if isinstance(m.clob_token_ids, list) and m.clob_token_ids:
                token_id = str(m.clob_token_ids[0])
            views.append(PolyView(
                market_id=m.market_id, condition_id=m.condition_id, token_id=token_id,
                question=m.question or "", domain=coarse_domain(m.question, m.category),
                outcome_type=normalize_outcome(m.question), tokens=title_tokens(m.question),
                resolution_time=_aware(m.end_date), midpoint=mid, spread=spread,
                liquidity_proxy=m.liquidity_usd,
            ))
        return views

    def _load_kalshi(self, session: Session, limit: int) -> list[KalshiView]:
        markets = session.execute(
            select(Market).where(Market.status == "active").limit(limit)
        ).scalars().all()
        if not markets:  # some datasets use "open"; fall back to most-recently-seen
            markets = session.execute(
                select(Market).order_by(Market.last_seen_at.desc()).limit(limit)
            ).scalars().all()
        views: list[KalshiView] = []
        now = _now()
        for mk in markets:
            snap = session.execute(
                select(MarketSnapshot).where(MarketSnapshot.market_id == mk.id)
                .order_by(MarketSnapshot.id.desc()).limit(1)
            ).scalars().first()
            mid = spread = liq = age = None
            if snap is not None:
                mid, spread = _kalshi_midpoint(snap)
                liq = float(snap.liquidity) if snap.liquidity is not None else None
                if snap.captured_at is not None:
                    age = (now - _aware(snap.captured_at)).total_seconds()
            res = _aware(mk.close_time) or _aware(mk.expiration_time)
            views.append(KalshiView(
                ticker=mk.ticker, event_ticker=mk.event_ticker, title=mk.title or "",
                domain=coarse_domain(mk.title, mk.category, mk.ticker),
                outcome_type=normalize_outcome(mk.title), tokens=title_tokens(mk.title),
                resolution_time=res, midpoint=mid, spread=spread, liquidity_proxy=liq,
                snapshot_age_seconds=age,
            ))
        return views

    def _best_match(self, p: PolyView, kalshi: list[KalshiView]) -> tuple[KalshiView | None, float]:
        best, best_sim = None, 0.0
        for k in kalshi:
            # gate by coarse domain (allow 'other' on either side, at lower weight)
            if p.domain != k.domain and "other" not in (p.domain, k.domain):
                continue
            sim = jaccard(p.tokens, k.tokens)
            if p.domain == k.domain and p.domain != "other":
                sim = round(min(1.0, sim + 0.05), 4)  # small same-domain bonus
            if sim > best_sim:
                best, best_sim = k, sim
        return best, best_sim

    def _label(self, p: PolyView, k: KalshiView, sim: float) -> tuple[str, float, list[str], list[str]]:
        match_reasons: list[str] = [f"title_similarity={sim}", f"domain={p.domain}"]
        mismatch: list[str] = []

        outcome_ok = outcomes_compatible(p.outcome_type, k.outcome_type)
        if outcome_ok:
            match_reasons.append(f"outcome_type={p.outcome_type}/{k.outcome_type}")
        else:
            mismatch.append(f"outcome_type_mismatch={p.outcome_type}!={k.outcome_type}")

        res_gap_days = None
        if p.resolution_time and k.resolution_time:
            res_gap_days = round(abs((p.resolution_time - k.resolution_time).total_seconds()) / 86400, 2)
            if res_gap_days <= RESOLUTION_PROXIMATE_DAYS:
                match_reasons.append(f"resolution_proximate_days={res_gap_days}")
            elif res_gap_days > RESOLUTION_MAX_DAYS:
                mismatch.append(f"resolution_gap_days={res_gap_days}")
        else:
            mismatch.append("resolution_time_missing")

        # confidence blends title similarity, outcome compatibility, resolution proximity
        conf = 0.6 * sim + (0.25 if outcome_ok else 0.0)
        if res_gap_days is not None and res_gap_days <= RESOLUTION_PROXIMATE_DAYS:
            conf += 0.15
        conf = round(min(1.0, conf), 4)

        if not outcome_ok:
            return LABEL_INCOMPATIBLE_OUTCOME, conf, match_reasons, mismatch
        if res_gap_days is not None and res_gap_days > RESOLUTION_MAX_DAYS:
            return LABEL_INCOMPATIBLE_RESOLUTION, conf, match_reasons, mismatch
        if sim >= HIGH_SIM and res_gap_days is not None and res_gap_days <= RESOLUTION_MAX_DAYS:
            return LABEL_COMPARABLE, conf, match_reasons, mismatch
        if sim >= LOW_SIM:
            return LABEL_LOW_CONFIDENCE, conf, match_reasons, mismatch
        return LABEL_UNRESOLVED, conf, match_reasons, mismatch

    def match_once(
        self, session: Session, kalshi_limit: int = 1500, polymarket_limit: int = 200, persist: bool = True
    ) -> CrossVenueObservationRun:
        started = _now()
        run = CrossVenueObservationRun(status="running", started_at=started, created_at=started)
        if persist:
            session.add(run)
            session.flush()

        try:
            polys = self._load_polymarket(session, polymarket_limit)
            kalshi = self._load_kalshi(session, kalshi_limit)
            comparable = unresolved = 0
            candidates: list[CrossVenueMarketCandidate] = []

            for p in polys:
                k, sim = self._best_match(p, kalshi)
                if k is None or sim < MIN_TITLE_SIM_FLOOR:
                    continue  # no plausible comparable — not persisted as noise
                label, conf, mreasons, mismatch = self._label(p, k, sim)

                # observation metrics (measurement only; midpoint diff only when the
                # two markets are outcome-compatible so the gap is meaningful)
                mid_diff = None
                if (
                    label in (LABEL_COMPARABLE, LABEL_LOW_CONFIDENCE)
                    and p.midpoint is not None and k.midpoint is not None
                ):
                    mid_diff = round(k.midpoint - p.midpoint, 4)
                obs_conf = self._observation_confidence(p, k)

                cand = CrossVenueMarketCandidate(
                    run_id=run.id if persist else None,
                    kalshi_ticker=k.ticker, kalshi_event_ticker=k.event_ticker,
                    polymarket_market_id=p.market_id, polymarket_token_id=p.token_id,
                    polymarket_condition_id=p.condition_id, domain=p.domain,
                    event_title_normalized=" | ".join([normalize_title(k.title), normalize_title(p.question)])[:512],
                    outcome_normalized=p.outcome_type,
                    resolution_time_kalshi=k.resolution_time,
                    resolution_time_polymarket=p.resolution_time,
                    match_confidence=conf, match_label=label,
                    match_reasons=mreasons, mismatch_reasons=mismatch,
                    kalshi_midpoint=k.midpoint, polymarket_midpoint=p.midpoint,
                    midpoint_difference=mid_diff,
                    kalshi_spread=k.spread, polymarket_spread=p.spread,
                    kalshi_liquidity_proxy=k.liquidity_proxy,
                    polymarket_liquidity_proxy=p.liquidity_proxy,
                    observed_difference=mid_diff, observation_confidence=obs_conf,
                    raw_context={"title_similarity": sim, "kalshi_snapshot_age_s": k.snapshot_age_seconds},
                    created_at=_now(),
                )
                candidates.append(cand)
                if label == LABEL_COMPARABLE:
                    comparable += 1
                elif label == LABEL_UNRESOLVED:
                    unresolved += 1

            if persist:
                for c in candidates:
                    session.add(c)

            run.status = "ok"
            run.finished_at = _now()
            run.duration_ms = int((run.finished_at - started).total_seconds() * 1000)
            run.kalshi_markets_considered = len(kalshi)
            run.polymarket_markets_considered = len(polys)
            run.candidates_created = len(candidates)
            run.comparable_count = comparable
            run.unresolved_count = unresolved
            run._candidates = candidates  # attached for non-persist callers/tests
            if persist:
                session.commit()
            return run
        except Exception as exc:
            logger.exception("cross-venue match_once failed: %s", exc)
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:500]
            run.finished_at = _now()
            if persist:
                try:
                    session.commit()
                except Exception:  # pragma: no cover
                    session.rollback()
            raise

    @staticmethod
    def _observation_confidence(p: PolyView, k: KalshiView) -> float:
        parts = [
            1.0 if p.midpoint is not None else 0.0,
            1.0 if k.midpoint is not None else 0.0,
            1.0 if (p.resolution_time and k.resolution_time) else 0.0,
            1.0 if (k.snapshot_age_seconds is not None and k.snapshot_age_seconds < 86400) else 0.0,
        ]
        return round(sum(parts) / len(parts), 4)


# --- report -----------------------------------------------------------------


def _pctile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((pct / 100) * (len(s) - 1)))))
    return round(s[idx], 4)


@dataclass
class CrossVenueReport:
    note: str
    last_run: dict | None
    candidates: int
    by_label: dict = field(default_factory=dict)
    by_domain: dict = field(default_factory=dict)
    comparable: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    mismatch_reasons: dict = field(default_factory=dict)
    midpoint_difference: dict = field(default_factory=dict)
    spread_liquidity: dict = field(default_factory=dict)
    freshness: dict = field(default_factory=dict)
    row_counts: dict = field(default_factory=dict)


class CrossVenueReportService:
    """Read-only aggregate over the latest cross-venue observation run."""

    def build(self, session: Session, top: int = 15) -> CrossVenueReport:
        last = session.execute(
            select(CrossVenueObservationRun)
            .where(CrossVenueObservationRun.status == "ok")
            .order_by(CrossVenueObservationRun.id.desc())
        ).scalars().first()

        cands: list[CrossVenueMarketCandidate] = []
        if last is not None:
            cands = session.execute(
                select(CrossVenueMarketCandidate)
                .where(CrossVenueMarketCandidate.run_id == last.id)
            ).scalars().all()

        by_label: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        mismatch: dict[str, int] = {}
        for c in cands:
            by_label[c.match_label] = by_label.get(c.match_label, 0) + 1
            by_domain[c.domain or "other"] = by_domain.get(c.domain or "other", 0) + 1
            for r in (c.mismatch_reasons or []):
                key = str(r).split("=")[0]
                mismatch[key] = mismatch.get(key, 0) + 1

        def row(c: CrossVenueMarketCandidate) -> dict:
            return {
                "kalshi_ticker": c.kalshi_ticker, "polymarket_market_id": c.polymarket_market_id,
                "domain": c.domain, "match_label": c.match_label, "match_confidence": c.match_confidence,
                "kalshi_midpoint": c.kalshi_midpoint, "polymarket_midpoint": c.polymarket_midpoint,
                "observed_difference": c.observed_difference,
                "observation_confidence": c.observation_confidence,
                "title": (c.event_title_normalized or "")[:80],
            }

        comparable = sorted(
            [c for c in cands if c.match_label == LABEL_COMPARABLE],
            key=lambda c: -(c.match_confidence or 0),
        )
        unresolved = [c for c in cands if c.match_label == LABEL_UNRESOLVED]

        diffs = [abs(c.observed_difference) for c in cands if c.observed_difference is not None]
        k_spreads = [c.kalshi_spread for c in cands if c.kalshi_spread is not None]
        p_spreads = [c.polymarket_spread for c in cands if c.polymarket_spread is not None]
        obs_conf = [c.observation_confidence for c in cands if c.observation_confidence is not None]

        return CrossVenueReport(
            note=DISCLAIMER,
            last_run=(
                {
                    "id": last.id, "status": last.status,
                    "kalshi_considered": last.kalshi_markets_considered,
                    "polymarket_considered": last.polymarket_markets_considered,
                    "candidates": last.candidates_created,
                    "comparable": last.comparable_count, "unresolved": last.unresolved_count,
                }
                if last else None
            ),
            candidates=len(cands),
            by_label=by_label,
            by_domain=by_domain,
            comparable=[row(c) for c in comparable[:top]],
            unresolved=[row(c) for c in unresolved[:top]],
            mismatch_reasons=dict(sorted(mismatch.items(), key=lambda kv: -kv[1])),
            midpoint_difference={
                "n": len(diffs),
                "abs_p50": _pctile(diffs, 50), "abs_p90": _pctile(diffs, 90),
                "abs_max": round(max(diffs), 4) if diffs else None,
                "note": "measured probability-point gap |kalshi_mid - polymarket_mid| — not EV/arbitrage",
            },
            spread_liquidity={
                "kalshi_spread_p50": _pctile(k_spreads, 50),
                "polymarket_spread_p50": _pctile(p_spreads, 50),
            },
            freshness={
                "observation_confidence_p50": _pctile(obs_conf, 50),
                "observation_confidence_p90": _pctile(obs_conf, 90),
            },
            row_counts={
                "cross_venue_observation_runs": session.execute(
                    select(func.count()).select_from(CrossVenueObservationRun)
                ).scalar() or 0,
                "cross_venue_market_candidates": session.execute(
                    select(func.count()).select_from(CrossVenueMarketCandidate)
                ).scalar() or 0,
            },
        )
