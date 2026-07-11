"""TENNIS-TAPE-001 — read-only synchronized tennis tape recorder (Phase 0).

Records replayable match tapes that align API-Tennis score/state observations
with Kalshi tennis market quote snapshots, so future research can measure how
market quotes move relative to match state. Both halves were independently
validated first: market ticks (TENNIS-WATCHER-001, live) and the score source
(TENNIS-PROVIDER-001, 73.9% live-candidate coverage).

Phase 0 = MEASUREMENT INFRASTRUCTURE ONLY:
- Manual bounded capture (`capture_once`): one score pass (hard call cap,
  deduped by date) + one market quote pass (existing read-only Kalshi
  adapter, chunked) + linking. `--dry-run` persists nothing. Not-dry-run
  persists ONLY tape rows (runs/score snapshots/market snapshots/links) —
  never signals, never watcher rows, never MarketOps state.
- No timer, no scheduled path, no flags enabled. The provider key is read
  from settings, reported present/absent, never printed or stored.
- Links carry honest confidence labels: source_backed_link /
  fuzzy_candidate / unresolved / provider_no_match /
  incompatible_market_type. Nothing is fabricated.

No probability models, Markov models, EV, paper trading, recommendations,
sizing, orders, wallets, signing, swaps, execution, or autonomy — and no
forecast/gate/promotion/MarketOps/EDGE-AUTO behavior change.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.kalshi import KalshiRestAdapter
from app.config import Settings, get_settings
from app.models import (
    TennisTapeLink,
    TennisTapeMarketSnapshot,
    TennisTapeRun,
    TennisTapeScoreSnapshot,
)
from app.services.edge_followthrough import _aware, _mean, _rate
from app.services.tennis_live_source import classify_tennis_market
from app.services.tennis_research import get_tennis_fetcher, parse_tennis_ticker
from app.services.tennis_watcher import discover_tennis_universe, rank_tennis_candidates

logger = logging.getLogger(__name__)

TAPE_NOTE = (
    "Read-only synchronized tennis tape: score/state observations aligned "
    "with market quote snapshots for replayable MEASUREMENT — how do quotes "
    "move relative to match state? Bounded manual capture only; nothing "
    "fabricated; provider key never printed. Not a model, not EV, not "
    "trading, never advice; no sizing, orders, wallets, keys, swaps, "
    "signing, or execution."
)

# hard caps per capture run
MAX_SCORE_CALLS = 4            # provider fixture calls (deduped by date)
LIVESCORE_CALLS = 1            # plus exactly one get_livescore call per run
MAX_MARKET_TICKERS = 200       # tickers per market quote pass

LINK_SOURCE_BACKED = "source_backed_link"
LINK_FUZZY = "fuzzy_candidate"
LINK_UNRESOLVED = "unresolved"
LINK_NO_MATCH = "provider_no_match"
LINK_INCOMPATIBLE = "incompatible_market_type"

STATUS_OK = "ok"
STATUS_DRY_RUN = "dry_run"
STATUS_PROVIDER_GAP = "skipped_provider_gap"
STATUS_NO_TARGETS = "no_targets"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fixture_codes(fixture: dict) -> tuple[str, str]:
    """Kalshi-style 3-letter codes from a raw fixture's player names."""
    def code(name):
        name = (name or "").strip()
        return name.split()[-1][:3].upper() if name else ""
    return code(fixture.get("event_first_player")), code(fixture.get("event_second_player"))


def _fixture_state(status_text: str | None) -> str:
    lowered = (status_text or "").lower()
    if not lowered:
        return "unknown"
    if lowered in ("finished", "retired", "walkover", "walk over"):
        return "post"
    if any(m in lowered for m in ("set", "live", "1st", "2nd", "3rd")):
        return "in"
    if lowered in ("cancelled", "canceled", "postponed"):
        return "unknown"
    return "pre"


@dataclass
class LinkOutcome:
    ticker: str
    label: str
    basis: str = ""
    fixture: dict | None = None
    player_a_code: str | None = None
    player_b_code: str | None = None
    event_date: str | None = None
    missing_info: list = field(default_factory=list)


def _adjacent_dates(date_str: str) -> list[str]:
    """[date, date+1, date-1] — Kalshi ticker dates and provider event dates
    can disagree by one day across timezones (measured live: KXITFMATCH
    26JUL09 tickers vs provider fixtures dated 2026-07-10)."""
    try:
        base = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return [date_str]
    return [
        date_str,
        (base + timedelta(days=1)).strftime("%Y-%m-%d"),
        (base - timedelta(days=1)).strftime("%Y-%m-%d"),
    ]


def link_candidate(ticker: str, fixtures_by_date: dict) -> LinkOutcome:
    """Pure linking: one Kalshi market ticker against the run's raw fixtures.
    Exact = both player codes on the ticker date, then on adjacent (+/-1 day)
    dates — timezone rollover is real and measured. Fuzzy = exactly one
    plausible single-code match on the exact date only. Never guesses
    beyond that."""
    if classify_tennis_market(ticker) != "match_winner":
        return LinkOutcome(ticker, LINK_INCOMPATIBLE,
                           basis="non-match-winner market")
    ctx = parse_tennis_ticker(ticker)
    if ctx is None:
        return LinkOutcome(ticker, LINK_UNRESOLVED, basis="ticker unparseable",
                           missing_info=["parseable ticker"])
    out = LinkOutcome(
        ticker, LINK_UNRESOLVED, event_date=ctx.date,
        player_a_code=ctx.player_a, player_b_code=ctx.player_b,
    )
    search_dates = [d for d in _adjacent_dates(ctx.date) if d in fixtures_by_date]
    if not search_dates:
        out.label = LINK_UNRESOLVED
        out.basis = "date (and adjacent days) not fetched this run (score call cap)"
        out.missing_info.append("provider fixtures for event date")
        return out
    if not ctx.player_a or not ctx.player_b:
        out.basis = "matchup fragment does not split into two codes"
        out.missing_info.append("player codes")
        return out
    want = {ctx.player_a, ctx.player_b}
    for search_date in search_dates:            # exact date first, then +/-1
        exact = []
        for f in fixtures_by_date.get(search_date) or []:
            a, b = _fixture_codes(f)
            if a and b and {a, b} == want:
                exact.append(f)
        if len(exact) == 1:
            out.label = LINK_SOURCE_BACKED
            out.basis = (
                "both player codes + date" if search_date == ctx.date
                else f"both player codes + adjacent date ({search_date})"
            )
            out.fixture = exact[0]
            return out
        if len(exact) > 1:
            out.label = LINK_FUZZY
            out.basis = f"{len(exact)} fixtures share both codes — ambiguous"
            out.fixture = exact[0]
            out.missing_info.append("unambiguous player-code match")
            return out
    partial = []
    for f in fixtures_by_date.get(ctx.date) or []:   # fuzzy: exact date only
        a, b = _fixture_codes(f)
        if not a or not b:
            continue
        if a in want or b in want:
            partial.append(f)
    if len(partial) == 1:
        out.label = LINK_FUZZY
        out.basis = "single one-sided player-code match"
        out.fixture = partial[0]
        out.missing_info.append("second player code match")
        return out
    out.label = LINK_NO_MATCH
    out.basis = "no fixture shares the ticker's player codes on or adjacent to the date"
    out.missing_info.append("provider event for this match")
    return out


def _strip_bulk(fixture: dict) -> dict:
    """Raw payload for the tape minus the unbounded point-by-point arrays
    (phase 0 stores match/set/game state; point-level is a later tier)."""
    return {k: v for k, v in fixture.items()
            if k not in ("pointbypoint", "statistics")}


class TennisTapeRecorder:
    """Manual bounded tape capture. Persists ONLY tape rows; never signals,
    never MarketOps state; dry-run persists nothing."""

    def __init__(
        self,
        score_fetcher=None,
        market_adapter: KalshiRestAdapter | None = None,
        settings: Settings | None = None,
    ):
        self.settings = settings or get_settings()
        self.score_fetcher = (
            score_fetcher if score_fetcher is not None
            else get_tennis_fetcher(self.settings)
        )
        self.market_adapter = market_adapter or KalshiRestAdapter()

    async def capture_once(
        self, session: Session, limit: int | None = None,
        hours: int = 24, dry_run: bool = False,
    ) -> dict:
        started = _now()
        fetcher = self.score_fetcher
        if fetcher is None or not getattr(fetcher, "has_key", False):
            return {
                "status": STATUS_PROVIDER_GAP,
                "note": (
                    "tape capture skipped: no score provider configured "
                    "(TENNIS_RESEARCH_PROVIDER/key absent) — a tape needs both "
                    "halves; nothing fetched, nothing persisted"
                ),
                "candidates": 0, "score_calls": 0, "market_fetches": 0,
                "score_snapshots": 0, "market_snapshots": 0, "links": {},
            }
        limit = min(limit if limit is not None else 50, MAX_MARKET_TICKERS)
        universe = discover_tennis_universe(session, hours=hours)
        # TENNIS-CANDIDATE-ORDER-001: bounded slots go to the most informative
        # books first (active/two-sided/high-volume/moving), not the alphabet
        ranked = rank_tennis_candidates(session, universe.active)[: max(limit, 0)]
        candidates = [c.market for c in ranked]
        if not candidates:
            return {
                "status": STATUS_NO_TARGETS,
                "note": "no active tennis markets in the recency window",
                "candidates": 0, "score_calls": 0, "market_fetches": 0,
                "score_snapshots": 0, "market_snapshots": 0, "links": {},
            }

        # --- score pass: raw fixtures per DISTINCT event date, hard-capped ----
        dates: list[str] = []
        for m in candidates:
            ctx = parse_tennis_ticker(m.ticker)
            if ctx and ctx.date not in dates:
                dates.append(ctx.date)
        fixtures_by_date: dict[str, list | None] = {}
        score_calls = 0
        for d in dates[:MAX_SCORE_CALLS]:
            payload = await fetcher._get({
                "method": "get_fixtures", "date_start": d, "date_stop": d,
            })
            score_calls += 1
            fixtures_by_date[d] = (payload or {}).get("result") if payload else None
            if fixtures_by_date[d] is None:
                logger.warning("tape: score fetch failed for %s", d)
        # one livescore overlay call: get_fixtures lags in-play state
        # (measured live: actively-traded matches showed live=0/status="" on
        # fixtures while get_livescore is the provider's now-playing view).
        # Live rows REPLACE same-event fixtures and are added to their own
        # event_date bucket so adjacent-date linking sees them too.
        live_payload = await fetcher._get({"method": "get_livescore"})
        score_calls += LIVESCORE_CALLS
        live_rows = (live_payload or {}).get("result") or []
        for row in live_rows:
            if not isinstance(row, dict):
                continue
            row_date = row.get("event_date")
            key = row.get("event_key")
            if not row_date:
                continue
            bucket = fixtures_by_date.get(row_date)
            if bucket is None:
                fixtures_by_date[row_date] = [row]
                continue
            for i, f in enumerate(bucket):
                if isinstance(f, dict) and f.get("event_key") == key:
                    bucket[i] = row
                    break
            else:
                bucket.append(row)

        # --- market pass: one chunked quote fetch over candidate tickers -----
        tickers = [m.ticker for m in candidates]
        quotes = {
            q.ticker: q
            for q in await self.market_adapter.fetch_markets_by_tickers(tickers)
        }
        market_observed = _now()

        # --- link ------------------------------------------------------------
        outcomes = [
            link_candidate(
                m.ticker,
                {d: f for d, f in fixtures_by_date.items() if f is not None},
            )
            for m in candidates
        ]
        label_mix: dict[str, int] = {}
        for o in outcomes:
            label_mix[o.label] = label_mix.get(o.label, 0) + 1

        summary = {
            "status": STATUS_DRY_RUN if dry_run else STATUS_OK,
            "note": TAPE_NOTE,
            "provider": getattr(fetcher, "source_name", "unknown"),
            "candidates": len(candidates),
            "score_calls": score_calls,
            "market_fetches": 1,
            "quotes_returned": len(quotes),
            "two_sided_quotes": sum(
                1 for q in quotes.values()
                if q.yes_bid is not None and q.yes_ask is not None
            ),
            "links": dict(sorted(label_mix.items(), key=lambda kv: -kv[1])),
            "top_ordering": [
                {"ticker": c.ticker, "reasons": c.reasons} for c in ranked[:5]
            ],
            "score_snapshots": 0,
            "market_snapshots": 0,
        }
        if dry_run:
            return summary

        # --- persist tape rows ONLY -------------------------------------------
        run = TennisTapeRun(
            status="running", started_at=started,
            provider_source=getattr(fetcher, "source_name", None),
            score_calls_made=score_calls, market_fetches_made=1,
            candidates_considered=len(candidates),
            created_at=started,
        )
        session.add(run)
        session.flush()

        score_rows: dict[str, TennisTapeScoreSnapshot] = {}   # event_key -> row
        market_rows: dict[str, TennisTapeMarketSnapshot] = {}
        score_observed = _now()
        titles = {m.ticker: m for m in candidates}
        for o in outcomes:
            market = titles.get(o.ticker)
            quote = quotes.get(o.ticker)
            m_row = market_rows.get(o.ticker)
            if m_row is None and quote is not None:
                m_row = TennisTapeMarketSnapshot(
                    tape_run_id=run.id, observed_at=market_observed,
                    market_ticker=o.ticker,
                    market_title=(market.title if market else None),
                    market_status=quote.status,
                    yes_bid=quote.yes_bid, yes_ask=quote.yes_ask,
                    midpoint=(
                        round((quote.yes_bid + quote.yes_ask) / 2 / 100, 4)
                        if quote.yes_bid is not None and quote.yes_ask is not None
                        else None
                    ),
                    spread=quote.spread, liquidity_proxy=quote.liquidity,
                    volume_24h=quote.volume_24h, created_at=market_observed,
                )
                session.add(m_row)
                session.flush()
                market_rows[o.ticker] = m_row
            s_row = None
            if o.fixture is not None:
                event_key = str(o.fixture.get("event_key") or f"anon-{o.ticker}")
                s_row = score_rows.get(event_key)
                if s_row is None:
                    missing = []
                    for field_name, col in (
                        ("event_status", "match_status"),
                        ("event_final_result", "final_result"),
                        ("event_game_result", "game_result"),
                        ("event_serve", "serving"),
                        ("scores", "set_scores"),
                    ):
                        if not o.fixture.get(field_name):
                            missing.append(col)
                    s_row = TennisTapeScoreSnapshot(
                        tape_run_id=run.id, observed_at=score_observed,
                        provider_source=getattr(fetcher, "source_name", "unknown"),
                        provider_event_id=str(o.fixture.get("event_key") or "") or None,
                        event_date=o.event_date,
                        event_type=o.fixture.get("event_type_type"),
                        tournament_name=o.fixture.get("tournament_name"),
                        player_a=o.fixture.get("event_first_player"),
                        player_b=o.fixture.get("event_second_player"),
                        match_status=o.fixture.get("event_status") or None,
                        match_state=_fixture_state(o.fixture.get("event_status")),
                        final_result=o.fixture.get("event_final_result") or None,
                        game_result=o.fixture.get("event_game_result") or None,
                        serving=o.fixture.get("event_serve") or None,
                        set_scores=o.fixture.get("scores") or None,
                        missing_info=missing or None,
                        raw_payload=_strip_bulk(o.fixture),
                        created_at=score_observed,
                    )
                    session.add(s_row)
                    session.flush()
                    score_rows[event_key] = s_row
            delta = None
            if s_row is not None and m_row is not None:
                delta = round((market_observed - score_observed).total_seconds(), 3)
            session.add(TennisTapeLink(
                tape_run_id=run.id,
                score_snapshot_id=(s_row.id if s_row else None),
                market_snapshot_id=(m_row.id if m_row else None),
                market_ticker=o.ticker,
                provider_event_id=(
                    str(o.fixture.get("event_key")) if o.fixture else None
                ),
                link_label=o.label, link_basis=o.basis,
                player_a_code=o.player_a_code, player_b_code=o.player_b_code,
                event_date=o.event_date,
                score_observed_at=(score_observed if s_row else None),
                market_observed_at=(market_observed if m_row else None),
                score_to_market_delta_s=delta,
                missing_info=o.missing_info or None,
                created_at=_now(),
            ))
        finished = _now()
        run.status = STATUS_OK
        run.finished_at = finished
        run.duration_ms = max(0, int((finished - started).total_seconds() * 1000))
        run.score_snapshots = len(score_rows)
        run.market_snapshots = len(market_rows)
        run.links_created = len(outcomes)
        run.source_backed_links = label_mix.get(LINK_SOURCE_BACKED, 0)
        session.commit()
        summary["score_snapshots"] = len(score_rows)
        summary["market_snapshots"] = len(market_rows)
        summary["tape_run_id"] = run.id
        return summary


# --- TENNIS-CAPTURE-SESSION-001: bounded manual session runner ---------------------
# A convenience wrapper over capture_once for live windows. NOT a timer, NOT a
# daemon: it runs a fixed, capped number of captures within one invocation and
# then exits. Aborts on abnormal capture status or detectable MarketOps error.

SESSION_MAX_DURATION_MIN = 60
SESSION_INTERVAL_MIN_S = 30
SESSION_INTERVAL_MAX_S = 300
SESSION_MAX_CAPTURES = 60

SESSION_OK = "ok"
SESSION_DRY_RUN = "dry_run"
SESSION_ABORTED = "aborted"


def _marketops_degraded(session: Session) -> bool:
    """Cheap detectable health check: latest MarketOps run errored."""
    try:
        from app.models import MarketOpsRun

        latest = session.execute(
            select(MarketOpsRun).order_by(MarketOpsRun.id.desc()).limit(1)
        ).scalars().first()
        return bool(latest is not None and latest.status == "error")
    except Exception:
        return False


def summarize_session(session: Session, run_ids: list[int], top: int = 5) -> dict:
    """Aggregate movement/link stats over the session's persisted tape runs.
    Read-only; empty for dry-run sessions (nothing persisted)."""
    if not run_ids:
        return {"available": False, "reason": "no persisted runs (dry-run session)"}
    links = session.execute(
        select(TennisTapeLink.link_label, TennisTapeLink.id)
        .where(TennisTapeLink.tape_run_id.in_(run_ids))
    ).all()
    label_mix: dict[str, int] = {}
    for label, _ in links:
        label_mix[label] = label_mix.get(label, 0) + 1
    snaps = session.execute(
        select(TennisTapeMarketSnapshot)
        .where(TennisTapeMarketSnapshot.tape_run_id.in_(run_ids))
        .order_by(TennisTapeMarketSnapshot.tape_run_id.asc())
    ).scalars().all()
    mids: dict[str, list] = {}
    two_sided = 0
    for s in snaps:
        if s.yes_bid is not None and s.yes_ask is not None:
            two_sided += 1
        if s.midpoint is not None:
            mids.setdefault(s.market_ticker, []).append(s.midpoint)
    movers = sorted(
        (
            {"ticker": t, "first_mid": series[0], "last_mid": series[-1],
             "abs_range": round(max(series) - min(series), 4)}
            for t, series in mids.items() if len(series) >= 2
        ),
        key=lambda m: -m["abs_range"],
    )
    scores = session.execute(
        select(TennisTapeScoreSnapshot.id)
        .where(TennisTapeScoreSnapshot.tape_run_id.in_(run_ids))
    ).all()
    return {
        "available": True,
        "runs": len(run_ids),
        "score_snapshots": len(scores),
        "market_snapshots": len(snaps),
        "links": dict(sorted(label_mix.items(), key=lambda kv: -kv[1])),
        "quote_coverage": (
            round(two_sided / len(snaps), 4) if snaps else None
        ),
        "top_movers": movers[:top],
        "db_impact_rows": len(run_ids) + len(scores) + len(snaps) + len(links),
    }


async def run_capture_session(
    session: Session,
    recorder: "TennisTapeRecorder | None" = None,
    duration_min: int = 15,
    interval_sec: int = 90,
    limit: int | None = None,
    dry_run: bool = False,
    top: int = 5,
) -> dict:
    """Bounded manual capture session: a fixed number of capture_once passes
    with a sleep between, then exit. Hard caps on duration/interval/captures;
    aborts on abnormal capture status or a detectable MarketOps error.
    Measurement only — never advice."""
    duration_min = max(1, min(duration_min, SESSION_MAX_DURATION_MIN))
    interval_sec = max(SESSION_INTERVAL_MIN_S, min(interval_sec, SESSION_INTERVAL_MAX_S))
    captures_planned = min(
        max(1, (duration_min * 60) // interval_sec), SESSION_MAX_CAPTURES
    )
    recorder = recorder or TennisTapeRecorder()
    started = _now()
    captures: list[dict] = []
    run_ids: list[int] = []
    provider_calls = 0
    abort_reason = None
    for i in range(captures_planned):
        result = await recorder.capture_once(
            session, limit=limit, dry_run=dry_run
        )
        captures.append(result)
        provider_calls += result.get("score_calls", 0)
        if result.get("tape_run_id"):
            run_ids.append(result["tape_run_id"])
        if result["status"] not in (STATUS_OK, STATUS_DRY_RUN):
            abort_reason = f"capture {i + 1} status={result['status']}"
            break
        if _marketops_degraded(session):
            abort_reason = "latest MarketOps run errored"
            break
        if i < captures_planned - 1:
            await asyncio.sleep(interval_sec)
    status = SESSION_ABORTED if abort_reason else (
        SESSION_DRY_RUN if dry_run else SESSION_OK
    )
    return {
        "note": TAPE_NOTE,
        "status": status,
        "abort_reason": abort_reason,
        "started_at": started.isoformat(),
        "duration_min": duration_min,
        "interval_sec": interval_sec,
        "captures_planned": captures_planned,
        "captures_run": len(captures),
        "provider_calls": provider_calls,
        "capture_statuses": [c["status"] for c in captures],
        "session_summary": summarize_session(session, run_ids, top=top),
        "tape_run_ids": run_ids,
    }


def build_tape_report(session: Session, hours: int = 24, top: int = 5) -> dict:
    """DB-only tape report: runs, snapshot volumes, link quality, freshness,
    deltas, examples. Read-only; no external call."""
    now = _now()
    cutoff = now - timedelta(hours=hours)
    runs = session.execute(
        select(TennisTapeRun).where(TennisTapeRun.started_at >= cutoff)
        .order_by(TennisTapeRun.id.desc())
    ).scalars().all()
    run_ids = [r.id for r in runs]
    links = session.execute(
        select(TennisTapeLink).where(TennisTapeLink.tape_run_id.in_(run_ids))
    ).scalars().all() if run_ids else []
    scores = session.execute(
        select(TennisTapeScoreSnapshot)
        .where(TennisTapeScoreSnapshot.tape_run_id.in_(run_ids))
    ).scalars().all() if run_ids else []
    markets = session.execute(
        select(TennisTapeMarketSnapshot)
        .where(TennisTapeMarketSnapshot.tape_run_id.in_(run_ids))
    ).scalars().all() if run_ids else []

    label_mix: dict[str, int] = {}
    for link in links:
        label_mix[link.link_label] = label_mix.get(link.link_label, 0) + 1
    latest_score_at = max((_aware(s.observed_at) for s in scores), default=None)
    latest_market_at = max((_aware(m.observed_at) for m in markets), default=None)
    linked_examples = [
        {
            "ticker": link.market_ticker, "label": link.link_label,
            "event_id": link.provider_event_id,
            "delta_s": link.score_to_market_delta_s,
        }
        for link in links if link.link_label == LINK_SOURCE_BACKED
    ][:top]
    unresolved_examples = [
        {"ticker": link.market_ticker, "label": link.link_label,
         "basis": link.link_basis}
        for link in links
        if link.link_label in (LINK_UNRESOLVED, LINK_NO_MATCH)
    ][:top]
    return {
        "note": TAPE_NOTE,
        "window_hours": hours,
        "generated_at": now.isoformat(),
        "tape_runs": len(runs),
        "score_snapshots": len(scores),
        "market_snapshots": len(markets),
        "links_total": len(links),
        "link_label_mix": dict(sorted(label_mix.items(), key=lambda kv: -kv[1])),
        "source_backed_rate": _rate(
            label_mix.get(LINK_SOURCE_BACKED, 0), len(links)
        ),
        "quote_coverage": _rate(
            sum(1 for m in markets
                if m.yes_bid is not None and m.yes_ask is not None),
            len(markets),
        ),
        "in_play_score_snapshots": sum(1 for s in scores if s.match_state == "in"),
        "score_freshness_s": (
            round((now - latest_score_at).total_seconds(), 1)
            if latest_score_at else None
        ),
        "market_freshness_s": (
            round((now - latest_market_at).total_seconds(), 1)
            if latest_market_at else None
        ),
        "mean_score_to_market_delta_s": _mean(
            [link.score_to_market_delta_s for link in links]
        ),
        "provider_gaps": (
            [] if any(r.provider_source for r in runs) else
            ["no tape runs with a configured provider in the window"]
        ),
        "linked_examples": linked_examples,
        "unresolved_examples": unresolved_examples,
        "db_impact_rows": len(links) + len(scores) + len(markets) + len(runs),
        "disclaimer": (
            "measurement only — replayable observation tape; not a model, "
            "not EV, not trading, never advice"
        ),
    }
