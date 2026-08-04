"""MarketOps Autopilot (OPS-006): 24/7 read-only coordination of the existing
market agents (Kalshi signal workflow, baseball/soccer research canaries,
crypto scout, outcome sync, calibration, champion/challenger).

One cycle = inspect signals -> auto-promote top-N -> process promoted ->
crypto scan -> sync outcomes -> score forecasts -> champion/challenger
snapshot -> local DB alerts -> one marketops_runs audit row. Every stage is
individually guarded: a failing stage records its error in the run summary
(and a provider_error alert) and the cycle continues unless fail_fast is set.

This layer creates NO new market capability — it only sequences existing
read-only services. No EV calculation, no paper trading, no trade
recommendations, no portfolio sizing, no order placement, no wallets/keys,
no swaps/transaction signing, no autonomous trading. Alerts are local DB
rows (no external Slack/Discord delivery in OPS-006). See
docs/SAFETY_BOUNDARIES.md.
"""

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import (
    CryptoOpportunitySignal,
    CryptoToken,
    MarketOpsAlert,
    MarketOpsRun,
    OpportunitySignal,
    WatcherRun,
)
from app.services.signal_workflow import (
    PROMOTION_PRIORITY,
    STATUS_FORECAST_REFRESHED,
    STATUS_NEW,
    STATUS_PROMOTED,
    SignalProcessingService,
    SignalPromotionService,
)

logger = logging.getLogger(__name__)

# Alert types (local DB only; no external delivery in OPS-006)
ALERT_SERVICE_HEALTH = "service_health_warning"
ALERT_TOO_MANY_SIGNALS = "too_many_signals"
ALERT_NO_RECENT_SIGNALS = "no_recent_signals"
ALERT_CRYPTO_SPIKE = "crypto_signal_spike"
ALERT_SOURCE_BACKED_FORECAST = "source_backed_forecast_created"
ALERT_CC_SAMPLE_UPDATE = "champion_challenger_sample_update"
ALERT_PROVIDER_ERROR = "provider_error"
ALERT_DB_GROWTH = "db_growth_warning"
# DB-GROWTH-ALERT-IDENTITY-001. ONE stable title, carrying no measurement.
# The pre-repair title embedded the rounded size (`Database at 4262 MiB`), and
# MarketOpsAlertService dedupes on (alert_type, title) — so every 1 MiB of
# growth minted a NEW open row instead of refreshing one. That produced 933 open
# rows with 933 distinct titles between 2026-07-05 and 2026-08-03, permanently
# pinning `marketops-report`'s recommended action and burying every other alert.
# The size now lives in the message and evidence, which are refreshed in place.
ALERT_DB_GROWTH_TITLE = "Database growth above threshold"
# The pre-repair identity, anchored and fullmatch'd — never a loose substring,
# and always paired with alert_type == ALERT_DB_GROWTH so it cannot reach an
# unrelated alert. Both the warning and critical spellings are covered.
# [0-9] rather than \d: \d matches every Unicode decimal digit, so an
# Arabic-Indic or fullwidth "Database at ٤٢٦٢ MiB" would match a title our code
# can never have produced. This decides which production rows get mutated.
LEGACY_DB_GROWTH_TITLE_RE = re.compile(r"Database at [0-9]+ MiB(?: \(critical\))?")
# SQLITE-BACKUP-FRESHNESS-ALERT-001. ONE stable title, deliberately carrying no
# varying data: MarketOpsAlertService dedupes on (alert_type, title), so a title
# that embeds a changing measurement stacks a new open row every time the
# measurement moves (db_growth_warning has accumulated hundreds of open rows
# that way). Reason, age and threshold live in the message/evidence instead, and
# a reason change UPDATES the single open row.
ALERT_BACKUP_FRESHNESS = "backup_freshness_warning"
ALERT_BACKUP_FRESHNESS_TITLE = "Backup protection unhealthy"

ALERT_STATUS_OPEN = "open"
ALERT_STATUS_RESOLVED = "resolved"

# Deterministic thresholds (shape of each rule; operational limits live in config).
# Signal-flood and DB-growth limits moved to config in OPS-011 (the values below
# are the pre-OPS-011 defaults, kept only as documentation of the old behavior).
NO_SIGNAL_WINDOW_HOURS = 6
WATCHER_STALE_MINUTES = 30
CRYPTO_SIGNAL_SPIKE_PER_CYCLE = 25
TICKER_REFRESH_COOLDOWN_SECONDS = 3600  # don't re-promote a just-refreshed ticker

# Domains whose promoted signals can currently become source-backed packets
SOURCE_BACKED_CAPABLE_DOMAINS = ("sports_baseball", "sports_soccer")

# OPS-009 promotion priority (measurement/promotion ordering ONLY — this is
# never an EV, value, or trade quantity):
MEASURABLE_MARKET_TYPES = ("spread", "total", "winner", "advance")
MARKET_TYPE_PLAYER = "player"
MARKET_TYPE_UNKNOWN = "unknown"
# Readiness-score weights (deterministic; sum bounds the score at ~100)
SCORE_FRESHNESS_MAX = 30.0
SCORE_SOURCE_BACKED_DOMAIN = 25.0
SCORE_MEASURABLE_MARKET_TYPE = 20.0
SCORE_UNKNOWN_MARKET_TYPE = 5.0
SCORE_SIGNAL_TYPE_STEP = 2.0  # (len(PROMOTION_PRIORITY) - index) * step
SCORE_BOOK_TWO_SIDED = 5.0
SCORE_BOOK_SPREAD_OK = 4.0
SCORE_BOOK_LIQUIDITY_OK = 4.0
SCORE_BOOK_FRESH_TICK = 2.0

# Player-code ticker segments look like ARGNGONZA11 / SEALRALEY20
_PLAYER_SEGMENT_RE = None  # compiled lazily


def _market_type_for_promotion(ticker: str, domain: str) -> str:
    """Deterministic market-type classification for promotion ordering:
    measurable types (spread/total/winner/advance) rank highest, unknown
    lower, player-prop markets lowest (team-level evidence cannot price a
    player — see SOCCER-002/MVP-004F)."""
    global _PLAYER_SEGMENT_RE
    import re as _re

    if _PLAYER_SEGMENT_RE is None:
        _PLAYER_SEGMENT_RE = _re.compile(r"^[A-Z]{4,}\d+$")

    if domain == "sports_soccer":
        from app.services.soccer_forecasting import parse_soccer_market_spec

        market_type = parse_soccer_market_spec(ticker).market_type
        if market_type == "player_goal":
            return MARKET_TYPE_PLAYER
        return market_type
    if domain == "sports_baseball":
        from app.services.baseball_forecasting import parse_market_spec

        market_type = parse_market_spec(ticker).market_type
        if market_type != "unknown":
            return market_type
    # player-code segment anywhere in the ticker => player market
    for segment in ticker.upper().split("-"):
        if _PLAYER_SEGMENT_RE.match(segment):
            return MARKET_TYPE_PLAYER
    if domain == "sports_baseball":
        return MARKET_TYPE_UNKNOWN
    series = ticker.upper().split("-", 1)[0]
    for market_type, markers in (
        ("total", ("TOTAL",)),
        ("spread", ("SPREAD", "HANDICAP")),
        ("winner", ("GAME", "MATCH", "WIN")),
        ("advance", ("ADVANCE",)),
    ):
        if any(marker in series for marker in markers):
            return market_type
    return MARKET_TYPE_UNKNOWN


@dataclass
class MarketOpsConfig:
    promote_limit: int = 5
    process_limit: int = 5
    crypto_scan_limit: int = 100
    sync_outcome_limit: int = 500
    score_limit: int = 1000
    min_signal_age_seconds: int = 30
    max_signal_age_hours: int = 24
    # OPS-009: minute-level, domain-aware freshness (supersedes the hour
    # knob; hours remain a coarse upper bound for compatibility)
    max_signal_age_minutes: int = 60
    live_sports_max_signal_age_minutes: int = 20
    soccer_max_signal_age_minutes: int = 20
    baseball_max_signal_age_minutes: int = 20
    general_max_signal_age_minutes: int = 60
    include_crypto: bool = True
    include_probability_markets: bool = True
    # MVP-005A: edge-precheck stage is DOUBLE-gated (this AND
    # ENABLE_EDGE_PRECHECK); both default false. Measurement only.
    include_edge_precheck: bool = False
    # CRYPTO-HORIZON-CANDIDATE-READINESS-001: isolated report-only post-crypto hook.
    # Default off; when off the hook is a complete no-op.
    include_candidate_readiness: bool = False
    # CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001: isolated provider-free
    # exact-cycle anchor materialization after crypto persistence, before the
    # readiness evaluation. Default off; when off the hook is a complete no-op.
    include_crypto_tape_anchor_feed: bool = False
    # SQLITE-BACKUP-FRESHNESS-ALERT-001: isolated local backup-health check in
    # the operational-health portion of the cycle. Default off; when off the
    # hook is a complete no-op.
    include_backup_freshness_alert: bool = False
    fail_fast: bool = False

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "MarketOpsConfig":
        s = settings or get_settings()
        return cls(
            promote_limit=s.marketops_promote_limit,
            process_limit=s.marketops_process_limit,
            crypto_scan_limit=s.marketops_crypto_scan_limit,
            sync_outcome_limit=s.marketops_sync_outcome_limit,
            score_limit=s.marketops_score_limit,
            min_signal_age_seconds=s.marketops_min_signal_age_seconds,
            max_signal_age_hours=s.marketops_max_signal_age_hours,
            max_signal_age_minutes=s.marketops_max_signal_age_minutes,
            live_sports_max_signal_age_minutes=(
                s.marketops_live_sports_max_signal_age_minutes
            ),
            soccer_max_signal_age_minutes=s.marketops_soccer_max_signal_age_minutes,
            baseball_max_signal_age_minutes=s.marketops_baseball_max_signal_age_minutes,
            general_max_signal_age_minutes=s.marketops_general_max_signal_age_minutes,
            include_crypto=s.marketops_include_crypto,
            include_probability_markets=s.marketops_include_probability_markets,
            include_edge_precheck=s.marketops_include_edge_precheck,
            include_candidate_readiness=s.marketops_include_candidate_readiness,
            include_crypto_tape_anchor_feed=s.marketops_include_crypto_tape_anchor_feed,
            include_backup_freshness_alert=s.marketops_include_backup_freshness_alert,
            fail_fast=s.marketops_fail_fast,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ticker_domain(ticker: str) -> str:
    """Deterministic domain from ticker prefix markers only (title/category
    are not loaded here; prefix rules cover every source-backed domain)."""
    from app.services.research import DOMAIN_GENERAL, DOMAIN_RULES

    upper = ticker.upper()
    for domain, markers, _keywords in DOMAIN_RULES:
        if any(upper.startswith(marker) for marker in markers):
            return domain
    return DOMAIN_GENERAL


def _priority_index(signal_type: str) -> int:
    try:
        return PROMOTION_PRIORITY.index(signal_type)
    except ValueError:
        return len(PROMOTION_PRIORITY)


class MarketOpsAlertService:
    """Local DB alerts with open-duplicate suppression."""

    def create(
        self,
        session: Session,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        evidence: dict | None = None,
    ) -> MarketOpsAlert | None:
        """Create an alert unless an identical (type, title) alert is already
        open — repeated cycles must not stack duplicates."""
        existing = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.title == title,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            )
        ).scalars().first()
        if existing is not None:
            return None
        alert = MarketOpsAlert(
            alert_type=alert_type,
            severity=severity,
            status=ALERT_STATUS_OPEN,
            title=title,
            message=message,
            evidence=evidence,
            created_at=_now(),
        )
        session.add(alert)
        session.flush()
        return alert

    def upsert_open(
        self,
        session: Session,
        alert_type: str,
        severity: str,
        title: str,
        message: str,
        evidence: dict | None = None,
    ) -> tuple[MarketOpsAlert, bool]:
        """Exactly-one-open-alert semantics for a stable (type, title) identity.

        Returns (alert, created). Unlike `create`, which returns None and drops
        the new information when an open duplicate exists, this refreshes the
        existing open row in place — so a recurring condition never stacks
        duplicates and a *changed* condition is still visible on the one row.
        Flushes rather than commits, so the write joins the caller's cycle
        transaction (`resolve` keeps its own commit for the interactive CLI).
        """
        open_rows = list(session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.title == title,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            ).order_by(MarketOpsAlert.id.asc())
        ).scalars().all())
        if open_rows:
            existing, extras = open_rows[0], open_rows[1:]
            existing.severity = severity
            existing.message = message
            existing.evidence = evidence
            # Converge to the invariant rather than assuming it. Two open rows
            # are reachable (the cycle overlap guard is a read-then-write check,
            # not a lock), and refreshing only the oldest would leave the others
            # open forever with stale evidence.
            resolved_at = _now()
            for extra in extras:
                extra.status = ALERT_STATUS_RESOLVED
                extra.resolved_at = resolved_at
            session.flush()
            return existing, False
        alert = MarketOpsAlert(
            alert_type=alert_type,
            severity=severity,
            status=ALERT_STATUS_OPEN,
            title=title,
            message=message,
            evidence=evidence,
            created_at=_now(),
        )
        session.add(alert)
        session.flush()
        return alert, True

    def has_open(self, session: Session, alert_type: str) -> bool:
        """Is any alert of this type currently open?"""
        return session.execute(
            select(MarketOpsAlert.id).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            ).limit(1)
        ).first() is not None

    def resolve_open_matching(
        self, session: Session, alert_type: str, title_matches
    ) -> int:
        """Resolve open alerts of `alert_type` whose title satisfies
        `title_matches`. Narrower than resolve_open_by_type: an automated hook
        should only ever close identities it is responsible for, never a
        hand-written row that happens to share the type."""
        open_alerts = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            )
        ).scalars().all()
        targets = [a for a in open_alerts if title_matches(a.title)]
        resolved_at = _now()
        for alert in targets:
            alert.status = ALERT_STATUS_RESOLVED
            alert.resolved_at = resolved_at
        if targets:
            session.flush()
        return len(targets)

    def resolve_open_by_type(self, session: Session, alert_type: str) -> int:
        """Resolve every open alert of `alert_type` through the existing
        lifecycle (status + resolved_at); returns how many were resolved.
        Flushes rather than commits, for the same reason as `upsert_open`."""
        open_alerts = session.execute(
            select(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == alert_type,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            )
        ).scalars().all()
        resolved_at = _now()
        for alert in open_alerts:
            alert.status = ALERT_STATUS_RESOLVED
            alert.resolved_at = resolved_at
        if open_alerts:
            session.flush()
        return len(open_alerts)

    def resolve(self, session: Session, alert_id: int) -> MarketOpsAlert:
        alert = session.get(MarketOpsAlert, alert_id)
        if alert is None:
            raise LookupError(f"Alert {alert_id} not found")
        if alert.status != ALERT_STATUS_RESOLVED:
            alert.status = ALERT_STATUS_RESOLVED
            alert.resolved_at = _now()
            session.commit()
        return alert

    def list_recent(
        self, session: Session, limit: int = 20, status: str | None = None
    ) -> list[MarketOpsAlert]:
        query = select(MarketOpsAlert).order_by(MarketOpsAlert.id.desc()).limit(limit)
        if status:
            query = query.where(MarketOpsAlert.status == status)
        return list(session.execute(query).scalars().all())


class MarketOpsAutopilotService:
    """One coordination cycle over existing read-only services. All
    collaborators are injectable for tests; defaults follow env flags."""

    def __init__(
        self,
        config: MarketOpsConfig | None = None,
        promotion_service: SignalPromotionService | None = None,
        processing_service: SignalProcessingService | None = None,
        crypto_service=None,
        outcome_service=None,
        calibration_service=None,
        champion_challenger_service=None,
        alert_service: MarketOpsAlertService | None = None,
        edge_precheck_service=None,
        anchor_feed_session_factory=None,
        alert_session_factory=None,
    ):
        self.config = config or MarketOpsConfig.from_settings()
        self.promotion_service = promotion_service or SignalPromotionService()
        self.processing_service = processing_service or SignalProcessingService()
        self._crypto_service = crypto_service
        self._outcome_service = outcome_service
        self._calibration_service = calibration_service
        self._cc_service = champion_challenger_service
        self._edge_service = edge_precheck_service
        self.alert_service = alert_service or MarketOpsAlertService()
        # ANCHOR-FEED-MEASUREMENT-001: injectable for tests; defaults to the
        # app sessionmaker (an isolated short-lived session per cycle).
        self._anchor_feed_session_factory = anchor_feed_session_factory
        # Isolated short-lived session factory for the operational-health alert
        # writes (steps 7a and 7b). Injectable for tests; defaults to the app
        # sessionmaker, so the shared cycle session is never touched by an alert
        # write and cannot be left needing rollback by one.
        self._alert_session_factory = alert_session_factory

    # --- stage helpers -----------------------------------------------------

    def _age_window_minutes(self, domain: str) -> float:
        """Effective per-domain freshness window (OPS-009): minutes supersede
        the hour knob, which survives only as a coarse upper bound."""
        cfg = self.config
        hour_bound = cfg.max_signal_age_hours * 60
        if domain == "sports_baseball":
            return min(cfg.baseball_max_signal_age_minutes, hour_bound)
        if domain == "sports_soccer":
            return min(cfg.soccer_max_signal_age_minutes, hour_bound)
        if domain.startswith("sports_"):
            return min(cfg.live_sports_max_signal_age_minutes, hour_bound)
        return min(
            cfg.general_max_signal_age_minutes, cfg.max_signal_age_minutes, hour_bound
        )

    def _eligible_signals(
        self, session: Session, now: datetime
    ) -> tuple[list[OpportunitySignal], int]:
        """(fresh 'new' signals inside their DOMAIN-SPECIFIC age window,
        stale-skipped count). Dismissed/reviewed/errored signals are excluded
        by the status filter."""
        cfg = self.config
        newest = now - timedelta(seconds=cfg.min_signal_age_seconds)
        widest = now - timedelta(
            minutes=max(
                cfg.max_signal_age_minutes,
                cfg.general_max_signal_age_minutes,
                cfg.live_sports_max_signal_age_minutes,
                cfg.soccer_max_signal_age_minutes,
                cfg.baseball_max_signal_age_minutes,
            )
        )
        rows = session.execute(
            select(OpportunitySignal).where(
                OpportunitySignal.signal_status == STATUS_NEW,
                OpportunitySignal.processing_error_type.is_(None),
                OpportunitySignal.observed_at <= newest,
                OpportunitySignal.observed_at >= widest,
            )
        ).scalars().all()
        eligible: list[OpportunitySignal] = []
        skipped_stale = 0
        for signal in rows:
            domain = _ticker_domain(signal.market_ticker)
            window = timedelta(minutes=self._age_window_minutes(domain))
            observed = _aware(signal.observed_at)
            if observed is not None and (now - observed) <= window:
                eligible.append(signal)
            else:
                skipped_stale += 1
        return eligible, skipped_stale

    def _measurement_readiness_score(
        self,
        session: Session,
        signal: OpportunitySignal,
        domain: str,
        market_type: str,
        now: datetime,
    ) -> tuple[float, dict]:
        """Deterministic promotion-ordering score (0..~100). Measurement
        readiness ONLY: how likely this signal's refresh is to produce a
        source-backed forecast that edge-precheck can validly measure. Never
        an EV/value/trade quantity."""
        from app.services.watcher import latest_tick_for

        cfg = self.config
        settings = get_settings()
        parts: dict = {}

        window_s = self._age_window_minutes(domain) * 60
        observed = _aware(signal.observed_at)
        age_s = (now - observed).total_seconds() if observed else window_s
        parts["freshness"] = round(
            SCORE_FRESHNESS_MAX * max(0.0, 1 - age_s / window_s), 2
        )

        parts["source_backed_domain"] = (
            SCORE_SOURCE_BACKED_DOMAIN if domain in SOURCE_BACKED_CAPABLE_DOMAINS else 0.0
        )

        if market_type in MEASURABLE_MARKET_TYPES:
            parts["market_type"] = SCORE_MEASURABLE_MARKET_TYPE
        elif market_type == MARKET_TYPE_PLAYER:
            parts["market_type"] = 0.0  # player props: lowest unless
            # player-specific evidence exists (none does in v1)
        else:
            parts["market_type"] = SCORE_UNKNOWN_MARKET_TYPE

        parts["signal_type"] = (
            len(PROMOTION_PRIORITY) - _priority_index(signal.signal_type)
        ) * SCORE_SIGNAL_TYPE_STEP

        book = 0.0
        tick = latest_tick_for(session, signal.market_ticker)
        if tick is not None:
            if tick.midpoint is not None:
                book += SCORE_BOOK_TWO_SIDED
            if (
                tick.spread is not None
                and tick.spread <= settings.edge_precheck_max_spread_cents
            ):
                book += SCORE_BOOK_SPREAD_OK
            if (
                tick.liquidity_proxy is not None
                and tick.liquidity_proxy >= settings.edge_precheck_min_liquidity_cents
            ):
                book += SCORE_BOOK_LIQUIDITY_OK
            tick_observed = _aware(tick.observed_at)
            if tick_observed is not None and (
                (now - tick_observed).total_seconds()
                <= settings.edge_precheck_max_market_snapshot_age_seconds
            ):
                book += SCORE_BOOK_FRESH_TICK
        parts["book_quality"] = book

        return round(sum(parts.values()), 2), parts

    def _recently_refreshed_tickers(self, session: Session, now: datetime) -> set[str]:
        cutoff = now - timedelta(seconds=TICKER_REFRESH_COOLDOWN_SECONDS)
        rows = session.execute(
            select(OpportunitySignal.market_ticker).where(
                OpportunitySignal.signal_status == STATUS_FORECAST_REFRESHED,
                OpportunitySignal.processed_at.is_not(None),
                OpportunitySignal.processed_at >= cutoff,
            )
        ).scalars().all()
        return set(rows)

    def _tickers_awaiting_processing(self, session: Session) -> set[str]:
        rows = session.execute(
            select(OpportunitySignal.market_ticker).where(
                OpportunitySignal.signal_status == STATUS_PROMOTED
            )
        ).scalars().all()
        return set(rows)

    def select_signals_for_promotion(
        self, session: Session, now: datetime | None = None
    ) -> tuple[list[OpportunitySignal], int, dict]:
        """Deterministic auto-promotion (OPS-009): candidates inside their
        domain-specific freshness window are ranked by a measurement-
        readiness score (freshness, source-backed capability, market-type
        measurability, signal-type priority, live book quality); at most one
        signal per ticker per cycle; tickers refreshed within the last hour
        or already awaiting processing are skipped. Returns
        (selected, total_seen, promotion_stats). The score orders promotion
        only — it is never an EV/value/trade quantity."""
        now = now or _now()
        candidates, skipped_stale = self._eligible_signals(session, now)
        seen = len(candidates)
        skip_tickers = self._recently_refreshed_tickers(session, now)
        skip_tickers |= self._tickers_awaiting_processing(session)

        # one candidate per ticker: best signal type, then newest
        best_per_ticker: dict[str, OpportunitySignal] = {}
        for signal in candidates:
            if signal.market_ticker in skip_tickers:
                continue
            current = best_per_ticker.get(signal.market_ticker)
            key = (_priority_index(signal.signal_type), -signal.id)
            if current is None or key < (
                _priority_index(current.signal_type), -current.id
            ):
                best_per_ticker[signal.market_ticker] = signal

        scored: list[tuple[float, dict, str, str, OpportunitySignal]] = []
        unmeasurable = 0
        for signal in best_per_ticker.values():
            domain = _ticker_domain(signal.market_ticker)
            market_type = _market_type_for_promotion(signal.market_ticker, domain)
            score, parts = self._measurement_readiness_score(
                session, signal, domain, market_type, now
            )
            if parts["book_quality"] == 0.0:
                unmeasurable += 1
            scored.append((score, parts, domain, market_type, signal))
        scored.sort(key=lambda item: (-item[0], -item[4].id))

        selected = scored[: self.config.promote_limit]
        ages = [
            (now - _aware(item[4].observed_at)).total_seconds()
            for item in selected
            if item[4].observed_at is not None
        ]
        stats = {
            "skipped_stale_count": skipped_stale,
            "unmeasurable_candidates": unmeasurable,
            "promoted_signal_age_s_mean": round(sum(ages) / len(ages), 1) if ages else None,
            "promoted_signal_age_s_max": round(max(ages), 1) if ages else None,
            "promoted_by_domain": {},
            "promoted_by_market_type": {},
            "promoted_by_signal_type": {},
            "readiness_scores": [item[0] for item in selected],
        }
        for score, _parts, domain, market_type, signal in selected:
            stats["promoted_by_domain"][domain] = (
                stats["promoted_by_domain"].get(domain, 0) + 1
            )
            stats["promoted_by_market_type"][market_type] = (
                stats["promoted_by_market_type"].get(market_type, 0) + 1
            )
            stats["promoted_by_signal_type"][signal.signal_type] = (
                stats["promoted_by_signal_type"].get(signal.signal_type, 0) + 1
            )
        return [item[4] for item in selected], seen, stats

    # --- lazily-built default collaborators --------------------------------

    @property
    def crypto_service(self):
        if self._crypto_service is None:
            from app.services.crypto_scout import CryptoDiscoveryService

            self._crypto_service = CryptoDiscoveryService()
        return self._crypto_service

    @property
    def outcome_service(self):
        if self._outcome_service is None:
            from app.services.outcomes import OutcomeService

            self._outcome_service = OutcomeService()
        return self._outcome_service

    @property
    def calibration_service(self):
        if self._calibration_service is None:
            from app.services.calibration import CalibrationService

            self._calibration_service = CalibrationService()
        return self._calibration_service

    @property
    def cc_service(self):
        if self._cc_service is None:
            from app.services.champion_challenger import ChampionChallengerService

            self._cc_service = ChampionChallengerService()
        return self._cc_service

    # --- the cycle ----------------------------------------------------------

    def _active_run(self, session: Session) -> MarketOpsRun | None:
        """The current non-stale 'running' cycle, if any (OPS-007 overlap
        lock, mirroring the baseline pipeline). Runs stuck in 'running'
        longer than MARKETOPS_LOCK_STALE_AFTER_MINUTES are treated as stale
        (crashed) and never wedge the system."""
        stale_cutoff = _now() - timedelta(
            minutes=get_settings().marketops_lock_stale_after_minutes
        )
        candidates = session.execute(
            select(MarketOpsRun)
            .where(MarketOpsRun.status == "running")
            .order_by(MarketOpsRun.id.desc())
        ).scalars().all()
        for row in candidates:
            started = _aware(row.started_at)
            if started is not None and started >= stale_cutoff:
                return row
        return None

    async def run_once(self, session: Session) -> MarketOpsRun:
        """One autopilot cycle. Stage failures are captured per stage in the
        run summary and the cycle continues (unless fail_fast); only setup
        failures mark the whole run as error. A concurrent active cycle
        (e.g. the timer firing during a manual run) yields a graceful
        'skipped' run instead of a lock collision."""
        cfg = self.config
        started_at = _now()

        active = self._active_run(session)
        if active is not None:
            skipped = MarketOpsRun(
                status="skipped",
                started_at=started_at,
                finished_at=started_at,
                duration_ms=0,
                config=asdict(cfg),
                summary={"reason": "already_running", "active_run_id": active.id},
                created_at=started_at,
            )
            session.add(skipped)
            session.commit()
            return skipped

        run = MarketOpsRun(
            status="running",
            started_at=started_at,
            config=asdict(cfg),
            created_at=started_at,
        )
        session.add(run)
        session.commit()

        summary: dict = {"stages": {}, "stage_errors": {}}
        alerts_created = 0

        async def stage(name: str, coro_factory):
            nonlocal alerts_created
            try:
                result = await coro_factory()
                summary["stages"][name] = "ok"
                return result
            except Exception as exc:
                logger.exception("MarketOps stage %r failed", name)
                summary["stages"][name] = "error"
                summary["stage_errors"][name] = f"{type(exc).__name__}: {str(exc)[:500]}"
                alert = self.alert_service.create(
                    session,
                    ALERT_PROVIDER_ERROR,
                    "warning",
                    f"MarketOps stage failed: {name}",
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                    evidence={"stage": name, "run_id": run.id},
                )
                if alert is not None:
                    alerts_created += 1
                if cfg.fail_fast:
                    raise
                return None

        try:
            now = _now()
            processed: list = []  # this cycle's refreshed signals

            # 1-3. Probability-market lane: inspect -> promote -> process
            if cfg.include_probability_markets:

                async def promote():
                    selected, seen, promo_stats = self.select_signals_for_promotion(
                        session, now
                    )
                    run.signals_seen = seen
                    summary["promotion"] = promo_stats
                    promoted = [
                        self.promotion_service.promote(session, signal.id)
                        for signal in selected
                    ]
                    return promoted

                promoted = await stage("promote_signals", promote) or []
                run.signals_promoted = len(promoted)

                async def process():
                    return await self.processing_service.process_promoted(
                        session, limit=cfg.process_limit
                    )

                processed = await stage("process_promoted", process) or []
                run.signals_processed = len(processed)
                summary["processed_tickers"] = [s.market_ticker for s in processed]

                # Informational alert for each source-backed refresh this cycle
                from app.services.signal_workflow import refreshed_packet_summary

                for signal in processed:
                    packet_summary = refreshed_packet_summary(session, signal)
                    if packet_summary and packet_summary.evidence_depth == "source_backed":
                        alert = self.alert_service.create(
                            session,
                            ALERT_SOURCE_BACKED_FORECAST,
                            "info",
                            f"Source-backed refresh: {signal.market_ticker}",
                            f"Signal #{signal.id} refreshed with "
                            f"{packet_summary.collector_name} "
                            f"(completeness {packet_summary.research_completeness_score})",
                            evidence={
                                "signal_id": signal.id,
                                "packet_id": packet_summary.packet_id,
                                "collector": packet_summary.collector_name,
                            },
                        )
                        if alert is not None:
                            alerts_created += 1
            else:
                summary["stages"]["probability_markets"] = "skipped"

            # 4. Crypto lane
            crypto_run = None
            if cfg.include_crypto:

                async def crypto():
                    # GATE-001: MarketOps is not exempt from provider
                    # authorization. Build an explicit policy that reproduces
                    # today's effective behavior — same enabled providers, same
                    # paid providers (confirmed), same caps, same scan output —
                    # so no operational behavior changes, only that the run is
                    # now explicitly governed. No scheduling/flag/.env change.
                    from app.config import get_settings
                    from app.services.crypto_provider_policy import (
                        ProviderPolicy,
                        new_run_id,
                        provider_run,
                    )

                    policy = ProviderPolicy.compatibility_from_settings(
                        get_settings(), run_id=new_run_id(), limit=cfg.crypto_scan_limit
                    )
                    with provider_run(policy):
                        return await self.crypto_service.scan_once(
                            session, limit=cfg.crypto_scan_limit, policy=policy
                        )

                crypto_run = await stage("crypto_scan", crypto)
                if crypto_run is not None:
                    run.crypto_tokens_seen = crypto_run.tokens_checked
                    run.crypto_signals_created = crypto_run.signals_created
                    if crypto_run.signals_created >= CRYPTO_SIGNAL_SPIKE_PER_CYCLE:
                        alert = self.alert_service.create(
                            session,
                            ALERT_CRYPTO_SPIKE,
                            "warning",
                            f"Crypto signal spike: {crypto_run.signals_created} in one scan",
                            f"Scan #{crypto_run.id} created {crypto_run.signals_created} "
                            f"signals (threshold {CRYPTO_SIGNAL_SPIKE_PER_CYCLE})",
                            evidence={"crypto_run_id": crypto_run.id},
                        )
                        if alert is not None:
                            alerts_created += 1
            else:
                summary["stages"]["crypto_scan"] = "skipped"

            # 4a. CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 — isolated,
            # provider-free, exact-cycle anchor materialization AFTER the crypto
            # stage has persisted and committed its discovery results and BEFORE
            # the readiness evaluation, so readiness can inspect the newly
            # materialized anchors in the same natural cycle. Uses the existing
            # lifecycle-tape logic on an isolated short-lived session (never the
            # shared cycle session), starts no scan, calls no provider, creates
            # no cohort/observation/unit, runs at most once per cycle, is
            # idempotent on replay, and CANNOT fail the cycle: any exception is
            # caught (never re-raised, even under fail_fast) and recorded.
            # Default-off; a complete no-op when off.
            if cfg.include_crypto_tape_anchor_feed and cfg.include_crypto:
                # Checkpoint-commit the shared session FIRST (run counters and
                # any crypto spike alert may be flushed-but-uncommitted, which
                # would hold the SQLite write lock this same coroutine's
                # isolated feed session must acquire — self-contention that can
                # never resolve). Consistent with the documented
                # "checkpoint-committed, not atomic" cycle contract; guarded by
                # the flag so the disabled path is byte-identical to before.
                session.commit()
                self._materialize_cycle_anchors(crypto_run, summary)

            # 4b. CRYPTO-HORIZON-CANDIDATE-READINESS-001 — isolated, non-blocking,
            # report-only measurement AFTER crypto persistence. It reads persisted
            # local data only (zero provider calls, no second scan), creates no
            # cohort/observation/unit, and CANNOT fail the cycle: any exception is
            # caught here (never re-raised, even under fail_fast) and recorded, so
            # it can never change stage eligibility, provider behavior, the cycle
            # result, or the exit code. Default-off; a complete no-op when off.
            if cfg.include_candidate_readiness and cfg.include_crypto:
                self._evaluate_candidate_readiness(session, run, summary)

            # 5. Outcome sync + scoring (safe: read-only GETs + local scoring)
            async def sync():
                synced = await self.outcome_service.sync_known_markets(
                    session, limit=cfg.sync_outcome_limit
                )
                return len(synced)

            run.outcomes_synced = await stage("sync_outcomes", sync) or 0

            async def score():
                counts = self.calibration_service.score_unscored(
                    session, limit=cfg.score_limit
                )
                summary["score_counts"] = counts
                return counts["scored"]

            run.forecasts_scored = await stage("score_forecasts", score) or 0

            # 5b. Edge precheck (MVP-005A.1) — measurement only, DOUBLE-gated
            # (MARKETOPS_INCLUDE_EDGE_PRECHECK AND ENABLE_EDGE_PRECHECK) and
            # strictly CYCLE-SCOPED: only forecasts refreshed by THIS cycle's
            # processed signals are measured — never a broad sweep. Nothing
            # downstream branches on the results.
            if cfg.include_edge_precheck and get_settings().enable_edge_precheck:

                async def edge():
                    from app.services.edge_precheck import (
                        EdgePrecheckService,
                        summarize_snapshots,
                    )

                    cycle_forecast_ids = [
                        signal.refreshed_forecast_id
                        for signal in processed
                        if signal.refreshed_forecast_id is not None
                    ]
                    service = self._edge_service or EdgePrecheckService()
                    snapshots = service.create_for_forecast_ids(
                        session, cycle_forecast_ids
                    )
                    summary["edge_precheck"] = summarize_snapshots(snapshots)
                    return len(snapshots)

                await stage("edge_precheck", edge)
            elif cfg.include_edge_precheck:
                summary["stages"]["edge_precheck"] = "skipped"  # engine flag off

            # 6. Champion/challenger snapshot (+ sample-update alert)
            async def compare():
                comparison = self.cc_service.compare(session)
                snapshot = {
                    "baseline": comparison.baseline_forecaster,
                    "challenger": comparison.challenger_forecaster,
                    "pair_count": comparison.paired.pair_count if comparison.paired else 0,
                    "sample_label": (
                        comparison.paired.sample_label
                        if comparison.paired
                        else comparison.sample_label
                    ),
                    "mean_delta_brier": (
                        comparison.paired.mean_delta_brier if comparison.paired else None
                    ),
                }
                summary["champion_challenger"] = snapshot
                return snapshot

            cc_snapshot = await stage("champion_challenger", compare)
            if cc_snapshot is not None:
                # compare against the last run that actually carried a
                # snapshot (skipped/errored runs don't)
                previous_rows = session.execute(
                    select(MarketOpsRun.summary)
                    .where(MarketOpsRun.id != run.id, MarketOpsRun.summary.is_not(None))
                    .order_by(MarketOpsRun.id.desc())
                    .limit(20)
                ).scalars().all()
                previous_pairs = 0
                for prev_summary in previous_rows:
                    snapshot = (prev_summary or {}).get("champion_challenger")
                    if snapshot is not None:
                        previous_pairs = snapshot.get("pair_count", 0)
                        break
                if cc_snapshot["pair_count"] != previous_pairs:
                    alert = self.alert_service.create(
                        session,
                        ALERT_CC_SAMPLE_UPDATE,
                        "info",
                        f"Champion/challenger pairs: {previous_pairs} -> "
                        f"{cc_snapshot['pair_count']}",
                        f"Paired sample now {cc_snapshot['pair_count']} "
                        f"({cc_snapshot['sample_label']}), "
                        f"mean_delta_brier={cc_snapshot['mean_delta_brier']}",
                        evidence=cc_snapshot,
                    )
                    if alert is not None:
                        alerts_created += 1

            # 7. Health / hygiene alerts
            alerts_created += self._health_alerts(session, now)

            # Operational-health checkpoint. Steps 7a/7b each write their alert
            # on an ISOLATED short-lived session, so the shared session must let
            # go of the SQLite write lock first — otherwise this same coroutine
            # self-contends and can never resolve. Consistent with the
            # documented "checkpoint-committed, not atomic" cycle contract
            # (stage 4a). Everything above — the run row, the health alerts — is
            # already final here; only summary/status/timings follow.
            session.commit()

            # 7a. DB-GROWTH-ALERT-IDENTITY-001 — one stable-identity database
            # growth alert. Same size calculation, same thresholds, same
            # severity bands as before; what changed is that the identity no
            # longer embeds the measurement, so repeated cycles refresh one row
            # instead of minting a new one per MiB. Fail-CONTAINED: any
            # exception is caught inside the helper and recorded, never
            # re-raised, even under fail_fast.
            alerts_created += self._evaluate_db_growth(summary)

            # 7b. SQLITE-BACKUP-FRESHNESS-ALERT-001 — local, provider-free
            # backup-protection health, adjacent to the db_growth_warning path
            # above. Inspects only local backup files/manifests; never executes
            # a backup, prunes, or modifies a backup artifact. Fail-CONTAINED:
            # any exception is caught inside the helper and recorded (never
            # re-raised, even under fail_fast), so it cannot change the cycle
            # result or exit code. Default-off; a complete no-op when off.
            #
            # The shared session was already checkpoint-committed above, for
            # the same write-lock reason.
            if cfg.include_backup_freshness_alert:
                alerts_created += self._evaluate_backup_freshness(summary)

            run.alerts_created = alerts_created
            run.summary = summary
            run.status = (
                "ok" if not summary["stage_errors"] else "partial"
            )
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            return run
        except Exception as exc:
            session.rollback()
            logger.exception("MarketOps cycle failed")
            run.status = "error"
            run.error_type = type(exc).__name__
            run.error_message = str(exc)[:2000]
            run.summary = summary
            run.alerts_created = alerts_created
            run.finished_at = _now()
            run.duration_ms = max(0, int((run.finished_at - started_at).total_seconds() * 1000))
            session.commit()
            return run

    def _materialize_cycle_anchors(self, crypto_run, summary: dict) -> None:
        """CRYPTO-HORIZON-ANCHOR-FEED-MEASUREMENT-001 — isolated provider-free
        exact-cycle anchor materialization. NEVER raises: any failure is
        recorded in the summary and swallowed so the MarketOps cycle is
        unaffected. Zero provider calls; no second scan; no cohort/observation/
        unit; bounded token count per cycle; idempotent on replay. Uses its own
        short-lived session so the shared cycle session's transaction
        boundaries are untouched. The bounded summary carries counts only —
        token-level provenance stays in the lifecycle tables."""
        import time as _time_mod

        started = _now()
        try:
            if crypto_run is None:
                summary["anchor_feed"] = {
                    "status": "skipped_no_crypto_run",
                    "source_crypto_run_id": None,
                    "external_calls": 0,
                    "duration_ms": 0,
                    "error": None,
                }
                return
            from app.services.crypto_tape import (
                DB_LOCKED_MAX_ATTEMPTS,
                DB_LOCKED_RETRY_SECONDS,
                CryptoLifecycleTapeRecorder,
                _is_db_locked,
                new_token_ids_for_run,
            )

            recorder = CryptoLifecycleTapeRecorder()
            if self._anchor_feed_session_factory is not None:
                feed_session = self._anchor_feed_session_factory()
            else:
                from app.db import get_sessionmaker

                feed_session = get_sessionmaker()()
            try:
                token_ids = new_token_ids_for_run(
                    feed_session, crypto_run.id, chain=recorder.config.chain
                )
                result = None
                last_exc: BaseException | None = None
                # canonical tape lock convention (same matcher/attempts/backoff
                # as the manual tape session) — never a new divergent ladder
                for attempt in range(1, DB_LOCKED_MAX_ATTEMPTS + 1):
                    try:
                        result = recorder.record_discovery_run(
                            feed_session, crypto_run.id, token_ids, dry_run=False
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        feed_session.rollback()
                        if _is_db_locked(exc) and attempt < DB_LOCKED_MAX_ATTEMPTS:
                            _time_mod.sleep(DB_LOCKED_RETRY_SECONDS)
                            continue
                        raise
                assert result is not None  # loop either set result or raised
                summary["anchor_feed"] = {
                    "status": result["status"],
                    "source_crypto_run_id": result["source_crypto_run_id"],
                    "tokens_received": result["tokens_received"],
                    "tokens_validated": result["tokens_validated"],
                    "anchors_attempted": result["anchors_attempted"],
                    "anchors_created": result["anchors_created"],
                    "anchors_existing": result["anchors_existing"],
                    "complete_anchors": result["complete_anchors"],
                    "incomplete_anchors": result["incomplete_anchors"],
                    "skipped_cap": result["skipped_cap"],
                    "external_calls": result["external_calls"],
                    "duration_ms": max(
                        0, int((_now() - started).total_seconds() * 1000)
                    ),
                    "error": result["error"],
                }
            finally:
                feed_session.close()
        except Exception as exc:  # never fail the cycle
            logger.warning("anchor-feed hook failed (isolated): %s", exc)
            summary["anchor_feed"] = {
                "status": "error",
                "source_crypto_run_id": getattr(crypto_run, "id", None),
                "external_calls": 0,
                "duration_ms": max(0, int((_now() - started).total_seconds() * 1000)),
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }

    def _evaluate_candidate_readiness(self, session: Session, run, summary: dict) -> None:
        """Isolated report-only readiness measurement. NEVER raises: any failure is
        recorded in the summary and swallowed so the MarketOps cycle is unaffected.
        Appends one append-only audit record per cycle. Zero provider calls; no
        cohort/observation/unit; cannot change the cycle result."""
        try:
            from app.services.crypto_horizon_readiness import (
                append_readiness_record,
                evaluate_readiness,
                readiness_audit_record,
            )

            readiness = evaluate_readiness(session, marketops_cycle_id=run.id)
            summary["candidate_readiness"] = {
                "state": readiness["state"],
                "overlapping_pairs": readiness["counts"]["overlapping_pairs"],
                "usable_pairs": readiness["counts"]["usable_pairs"],
                "complete_candidates": readiness["counts"]["complete_candidates"],
                "candidate_pair": (
                    [readiness["top_pair"]["token_a"], readiness["top_pair"]["token_b"]]
                    if readiness.get("top_pair") else None),
                "external_calls": 0,
            }
            append_readiness_record(readiness_audit_record(readiness, run_id=run.id))
        except Exception as exc:  # never fail the cycle
            logger.warning("candidate-readiness hook failed (isolated): %s", exc)
            summary["candidate_readiness_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"

    def _evaluate_db_growth(self, summary: dict) -> int:
        """DB-GROWTH-ALERT-IDENTITY-001 — one stable-identity database-growth
        alert per cycle.

        The size calculation, both thresholds and both severity bands are
        EXACTLY as before; this milestone changed only the alert's identity and
        lifecycle. Above critical -> critical, above warning -> warning, below
        warning -> resolve. Repeated unhealthy cycles refresh the single open
        row (and converge any same-title duplicates); a healthy cycle resolves
        every open row of this type, legacy duplicates included.

        NEVER raises: any failure is recorded in summary["db_growth_error"] and
        swallowed, so the MarketOps cycle is unaffected even under fail_fast.
        Crucially, a failure must NOT resolve anything — an evaluation that
        could not measure the database has no business closing a critical alert.

        Like the freshness hook, the alert write runs on its OWN short-lived
        session: a failed INSERT on the shared cycle session would leave it
        needing rollback and take the whole cycle's audit row down with it.
        """
        try:
            settings = get_settings()
            size_mb = database_size_mb(settings)
            warn_mb = settings.db_growth_warning_mb
            crit_mb = settings.db_growth_critical_mb

            if size_mb is None:
                # Unmeasurable (non-SQLite, or the file vanished). Report it and
                # change nothing — in particular, do not resolve.
                summary["db_growth"] = {
                    "status": "unavailable", "size_mb": None,
                    "warning_mb": warn_mb, "critical_mb": crit_mb,
                    "alert_action": "none", "external_calls": 0,
                }
                return 0

            if size_mb >= crit_mb:
                severity, threshold_mb, band = "critical", crit_mb, "critical"
            elif size_mb >= warn_mb:
                severity, threshold_mb, band = "warning", warn_mb, "warning"
            else:
                severity, threshold_mb, band = None, warn_mb, "ok"

            created = 0
            alert_session = self._new_alert_session()
            try:
                if severity is None:
                    # Title-SCOPED, not type-scoped: an unattended timer must
                    # not be able to close a hand-written, operator-pinned
                    # db_growth row it did not create. Only this milestone's own
                    # identities (canonical + strict legacy) are resolvable here.
                    resolved = self.alert_service.resolve_open_matching(
                        alert_session,
                        ALERT_DB_GROWTH,
                        lambda title: (
                            title == ALERT_DB_GROWTH_TITLE
                            or is_legacy_db_growth_title(title)
                        ),
                    )
                    action = "resolved" if resolved else "none"
                else:
                    # upsert_open REPLACES evidence wholesale, so carry the
                    # first-observation stamp forward or the very next cycle
                    # would erase what reconciliation preserved.
                    existing = alert_session.execute(
                        select(MarketOpsAlert).where(
                            MarketOpsAlert.alert_type == ALERT_DB_GROWTH,
                            MarketOpsAlert.title == ALERT_DB_GROWTH_TITLE,
                            MarketOpsAlert.status == ALERT_STATUS_OPEN,
                        ).order_by(MarketOpsAlert.id.asc())
                    ).scalars().first()
                    first_observed = None
                    if existing is not None:
                        first_observed = (existing.evidence or {}).get(
                            "condition_first_observed_at"
                        ) or (
                            existing.created_at.isoformat()
                            if existing.created_at else None
                        )
                    _alert, was_created = self.alert_service.upsert_open(
                        alert_session,
                        ALERT_DB_GROWTH,
                        severity,
                        ALERT_DB_GROWTH_TITLE,
                        _db_growth_message(size_mb, band, threshold_mb),
                        evidence=_db_growth_evidence(
                            size_mb, warn_mb, crit_mb, severity,
                            first_observed_at=first_observed,
                        ),
                    )
                    created = 1 if was_created else 0
                    action = "created" if was_created else "updated"
                alert_session.commit()
            finally:
                alert_session.close()

            summary["db_growth"] = {
                "status": band,
                "size_mb": round(size_mb, 2),
                "warning_mb": warn_mb,
                "critical_mb": crit_mb,
                "severity": severity,
                "alert_action": action,
                "external_calls": 0,
            }
            return created
        except Exception as exc:  # never fail the cycle, never resolve on failure
            logger.warning("db-growth hook failed (isolated): %s", exc)
            summary["db_growth_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return 0

    def _new_alert_session(self):
        """Isolated short-lived session for the operational-health alert writes.
        Injectable for tests; defaults to the app sessionmaker."""
        if self._alert_session_factory is not None:
            return self._alert_session_factory()
        from app.db import get_sessionmaker

        return get_sessionmaker()()

    def _evaluate_backup_freshness(self, summary: dict) -> int:
        """SQLITE-BACKUP-FRESHNESS-ALERT-001 — one bounded local backup-health
        evaluation per cycle, plus the deduplicated alert lifecycle.

        NEVER raises: any failure is recorded in `summary["backup_freshness_error"]`
        and swallowed, so the MarketOps cycle is unaffected even under fail_fast.
        Zero provider calls, one non-recursive scan of the canonical backup root,
        no SHA recomputation, no decompression, no backup execution, no pruning,
        no backup-file mutation.

        The alert lifecycle runs on its OWN short-lived session, mirroring
        `_materialize_cycle_anchors`. That is not incidental. Writing it on the
        shared cycle session — even inside a SAVEPOINT — is unsafe two ways:
        `begin_nested()` autoflushes the session's pending state BEFORE emitting
        the savepoint, so a transient "database is locked" on that flush would
        poison the shared session and fail the cycle's own final commit; and on
        pysqlite a SAVEPOINT opened when no transaction is in progress COMMITS
        on RELEASE, making the alert's transaction boundary depend on whether an
        earlier stage happened to leave DML pending. An isolated session removes
        both: the shared session is never touched, and the alert's durability is
        deterministic — it survives a later cycle failure, which is the right
        semantics for a monitoring signal.

        Returns the number of alerts CREATED (updates and resolutions are
        lifecycle transitions on the one existing row, not new alerts). The
        summary is bounded and carries no paths or manifest bodies.
        """
        try:
            from app.services.backup_freshness import (
                REASON_EVALUATION_ERROR,
                evaluate_backup_freshness,
            )

            result = evaluate_backup_freshness()
            created = 0
            alert_session = self._new_alert_session()
            try:
                if result.healthy:
                    # Quiet by design: no new alert, and any active freshness
                    # alert is resolved through the existing lifecycle.
                    resolved = self.alert_service.resolve_open_by_type(
                        alert_session, ALERT_BACKUP_FRESHNESS
                    )
                    action = "resolved" if resolved else "none"
                elif result.reason == REASON_EVALUATION_ERROR and self.alert_service.has_open(
                    alert_session, ALERT_BACKUP_FRESHNESS
                ):
                    # The CHECK broke while an alert is already open. Do not
                    # overwrite it: downgrading an outstanding critical
                    # "no committed backup" to a warning "the monitor hiccuped"
                    # would mask the real outage on the one row carrying it.
                    action = "preserved"
                else:
                    _alert, was_created = self.alert_service.upsert_open(
                        alert_session,
                        ALERT_BACKUP_FRESHNESS,
                        result.severity,
                        ALERT_BACKUP_FRESHNESS_TITLE,
                        (
                            f"No healthy backup: reason={result.reason}; "
                            f"newest_verified_at={result.newest_verified_at}; "
                            f"age_seconds={result.age_seconds}; "
                            f"threshold_seconds={result.threshold_seconds}. "
                            "Inspect BACKUP_DIR and the backup timer "
                            "(sqlite-backup-freshness-report)."
                        ),
                        evidence=result.summary_dict(),
                    )
                    created = 1 if was_created else 0
                    action = "created" if was_created else "updated"
                alert_session.commit()
            finally:
                alert_session.close()
            summary["backup_freshness"] = {
                **result.summary_dict(), "alert_action": action
            }
            return created
        except Exception as exc:  # never fail the cycle
            logger.warning("backup-freshness hook failed (isolated): %s", exc)
            summary["backup_freshness_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            return 0

    def _health_alerts(self, session: Session, now: datetime) -> int:
        """Deterministic health checks -> local alerts. Returns alerts created."""
        created = 0
        settings = get_settings()

        hour_ago = now - timedelta(hours=1)
        signals_last_hour = session.execute(
            select(func.count()).select_from(OpportunitySignal).where(
                OpportunitySignal.created_at >= hour_ago
            )
        ).scalar() or 0
        flood_warn = settings.marketops_signal_flood_warning_per_hour
        flood_crit = settings.marketops_signal_flood_critical_per_hour
        if signals_last_hour > flood_crit:
            if self.alert_service.create(
                session,
                ALERT_TOO_MANY_SIGNALS,
                "critical",
                f"Signal flood (critical): {signals_last_hour} signals in the last hour",
                f"Above critical {flood_crit}/h — verify the watcher isn't looping/mis-deduping",
                evidence={"signals_last_hour": signals_last_hour, "threshold": flood_crit},
            ):
                created += 1
        elif signals_last_hour > flood_warn:
            if self.alert_service.create(
                session,
                ALERT_TOO_MANY_SIGNALS,
                "warning",
                f"Signal flood: {signals_last_hour} signals in the last hour",
                f"Above warning {flood_warn}/h — normal for busy live slates; check cooldowns if sustained",
                evidence={"signals_last_hour": signals_last_hour, "threshold": flood_warn},
            ):
                created += 1

        window_start = now - timedelta(hours=NO_SIGNAL_WINDOW_HOURS)
        recent = session.execute(
            select(func.count()).select_from(OpportunitySignal).where(
                OpportunitySignal.created_at >= window_start
            )
        ).scalar() or 0
        if recent == 0 and settings.enable_realtime_watcher:
            if self.alert_service.create(
                session,
                ALERT_NO_RECENT_SIGNALS,
                "warning",
                f"No signals in {NO_SIGNAL_WINDOW_HOURS}h",
                "Watcher is enabled but produced no signals — verify the service and market hours",
                evidence={"window_hours": NO_SIGNAL_WINDOW_HOURS},
            ):
                created += 1

        if settings.enable_realtime_watcher:
            latest_watcher = session.execute(
                select(WatcherRun).order_by(WatcherRun.id.desc())
            ).scalars().first()
            stale_cutoff = now - timedelta(minutes=WATCHER_STALE_MINUTES)
            watcher_started = _aware(latest_watcher.started_at) if latest_watcher else None
            if (
                latest_watcher is None
                or watcher_started < stale_cutoff
                or latest_watcher.status == "error"
            ):
                detail = (
                    "no watcher runs recorded"
                    if latest_watcher is None
                    else f"latest run #{latest_watcher.id} status={latest_watcher.status} "
                    f"started={watcher_started}"
                )
                if self.alert_service.create(
                    session,
                    ALERT_SERVICE_HEALTH,
                    "warning",
                    "Watcher looks stale or errored",
                    f"{detail} (stale threshold {WATCHER_STALE_MINUTES}min)",
                    evidence={"stale_minutes": WATCHER_STALE_MINUTES},
                ):
                    created += 1

        # Database growth moved to _evaluate_db_growth (step 7a,
        # DB-GROWTH-ALERT-IDENTITY-001): it needs a stable identity, a
        # resolution path and failure isolation, none of which the plain
        # create()-only pattern above provides. Thresholds and severity
        # semantics are unchanged.
        return created


def _db_growth_evidence(
    size_mb: float, warn_mb: float, crit_mb: float, severity: str,
    *, first_observed_at: str | None = None,
) -> dict:
    """Bounded, secret-free evidence for the canonical database-growth alert.
    Everything that CHANGES lives here rather than in the title."""
    evidence = {
        "size_mb": round(size_mb, 2),
        "threshold_mb": crit_mb if severity == "critical" else warn_mb,
        "warning_mb": warn_mb,
        "critical_mb": crit_mb,
        "severity": severity,
        "above_warning": size_mb >= warn_mb,
        "above_critical": size_mb >= crit_mb,
        "observed_at": _now().isoformat(),
    }
    if first_observed_at is not None:
        # Carried forward from the earliest legacy duplicate so reconciliation
        # does not destroy when the condition actually started.
        evidence["condition_first_observed_at"] = first_observed_at
    return evidence


def _db_growth_message(size_mb: float, band: str, threshold_mb: float) -> str:
    """Single source for the alert message, so the cycle and the reconciliation
    can never drift apart."""
    return (
        f"SQLite file is {size_mb:.0f} MiB, above the {band} threshold of "
        f"{threshold_mb:.0f} MiB — review retention windows (db-growth-report)."
    )


def is_legacy_db_growth_title(title: str) -> bool:
    """Strict matcher for the pre-repair database-growth alert identity.

    Anchored fullmatch on the exact legacy spelling only. Callers ALWAYS pair
    this with alert_type == ALERT_DB_GROWTH, so a same-titled alert of another
    type could not be reached even if one existed. A substring match would be
    wrong here: this decides which production rows get mutated.
    """
    if not isinstance(title, str):
        # SQLite's dynamic typing makes a non-text title representable. Not ours
        # -> not matched, rather than raising in the middle of the read phase.
        return False
    return bool(LEGACY_DB_GROWTH_TITLE_RE.fullmatch(title))


def reconcile_db_growth_alerts(
    session: Session, *, confirm: bool = False, settings: Settings | None = None
) -> dict:
    """DB-GROWTH-ALERT-IDENTITY-001 — converge the legacy database-growth alert
    backlog onto ONE canonical row.

    Read-only unless `confirm` is set. Never hard-deletes: duplicates are
    RESOLVED through the existing lifecycle, so the full history stays queryable.
    Touches only rows whose alert_type is ALERT_DB_GROWTH *and* whose title is
    either the canonical title or a strict legacy match — every other row, of
    every other type (backup_freshness_warning included), is reported as
    excluded and never written.

    Canonical-row policy: prefer an existing open row already carrying the
    canonical title (lowest id); otherwise promote the OLDEST legacy row, so the
    alert keeps the created_at at which the condition was first observed. Either
    way the earliest observation across all matched rows is preserved in
    evidence as `condition_first_observed_at`.

    Makes NO application-data deletion, no provider call, and no backup,
    retention, cohort or observation action.
    """
    settings = settings or get_settings()
    size_mb = database_size_mb(settings)
    warn_mb = settings.db_growth_warning_mb
    crit_mb = settings.db_growth_critical_mb

    if size_mb is None:
        severity = None
        band = "unavailable"
    elif size_mb >= crit_mb:
        severity, band = "critical", "critical"
    elif size_mb >= warn_mb:
        severity, band = "warning", "warning"
    else:
        severity, band = None, "ok"

    # no_autoflush: on a caller-supplied session with pending state, the reads
    # below would otherwise autoflush that state to disk — a "read-only" dry run
    # must not write, even indirectly.
    with session.no_autoflush:
        open_rows = list(session.execute(
            select(MarketOpsAlert)
            .where(
                MarketOpsAlert.alert_type == ALERT_DB_GROWTH,
                MarketOpsAlert.status == ALERT_STATUS_OPEN,
            )
            .order_by(MarketOpsAlert.id.asc())
        ).scalars().all())
        resolved_legacy = session.execute(
            select(func.count()).select_from(MarketOpsAlert).where(
                MarketOpsAlert.alert_type == ALERT_DB_GROWTH,
                MarketOpsAlert.status == ALERT_STATUS_RESOLVED,
            )
        ).scalar() or 0

    canonical_rows = [a for a in open_rows if a.title == ALERT_DB_GROWTH_TITLE]
    legacy_rows = [a for a in open_rows if is_legacy_db_growth_title(a.title)]
    unmatched_rows = [
        a for a in open_rows
        if a.title != ALERT_DB_GROWTH_TITLE and not is_legacy_db_growth_title(a.title)
    ]

    matched = canonical_rows + legacy_rows
    first_observed = min((a.created_at for a in matched if a.created_at), default=None)
    canonical = canonical_rows[0] if canonical_rows else (
        min(legacy_rows, key=lambda a: (a.created_at or _now(), a.id))
        if legacy_rows else None
    )
    # An UNMEASURABLE database is not a healthy one. If the size could not be
    # read we still converge duplicates, but we keep the canonical row open and
    # leave its severity/evidence untouched — closing a critical alert on the
    # strength of a failed measurement is exactly the wrong direction.
    measurable = size_mb is not None
    keep_canonical = canonical is not None and (severity is not None or not measurable)
    refresh_canonical = keep_canonical and severity is not None
    to_resolve = [a for a in matched if not (keep_canonical and a is canonical)]

    report = {
        "confirmed": bool(confirm),
        "persisted": False,
        "external_calls": 0,
        "size_mb": round(size_mb, 2) if size_mb is not None else None,
        "warning_mb": warn_mb,
        "critical_mb": crit_mb,
        "severity": severity,
        "status": band,
        "canonical_title": ALERT_DB_GROWTH_TITLE,
        "legacy_title_pattern": LEGACY_DB_GROWTH_TITLE_RE.pattern,
        "open_total": len(open_rows),
        "matched_total": len(matched),
        "matched_canonical": len(canonical_rows),
        "matched_legacy": len(legacy_rows),
        "already_resolved": resolved_legacy,
        "excluded_unmatched": len(unmatched_rows),
        "excluded_unmatched_titles": [
            str(t)[:200] for t in sorted({a.title for a in unmatched_rows})[:10]
        ],
        "canonical_id": canonical.id if canonical else None,
        "canonical_created_at": (
            canonical.created_at.isoformat()
            if canonical is not None and canonical.created_at else None
        ),
        "canonical_source": (
            None if canonical is None
            else "existing_canonical" if canonical_rows else "promoted_legacy"
        ),
        "condition_first_observed_at": (
            first_observed.isoformat() if first_observed else None
        ),
        "would_resolve": len(to_resolve),
        "would_resolve_ids": [a.id for a in to_resolve][:20],
        "would_resolve_id_range": (
            [min(a.id for a in to_resolve), max(a.id for a in to_resolve)]
            if to_resolve else None
        ),
        "measurable": measurable,
        "canonical_refreshed": refresh_canonical,
        "hard_deletes": 0,
        # The ACTUAL post-state, unmatched rows included — this is the number an
        # operator uses to decide, so it must not under-report.
        "remaining_open_after": (1 if keep_canonical else 0) + len(unmatched_rows),
        "remaining_open_matched_after": 1 if keep_canonical else 0,
        "remaining_open_unmatched_after": len(unmatched_rows),
        # Full id set + the exact stamp, so the operation has a precise inverse:
        #   UPDATE marketops_alerts SET status='open', resolved_at=NULL
        #    WHERE alert_type='db_growth_warning' AND resolved_at='<resolved_at>';
        "resolve_ids": [a.id for a in to_resolve],
        "resolved_at": None,
    }

    if not confirm:
        # Dry run: guarantee nothing was written, even accidentally.
        session.rollback()
        return report

    try:
        resolved_at = _now()
        report["resolved_at"] = resolved_at.isoformat()
        if keep_canonical:
            # Always adopt the canonical identity, so the next natural cycle
            # refreshes this row instead of minting another.
            canonical.title = ALERT_DB_GROWTH_TITLE
        if refresh_canonical:
            canonical.severity = severity
            canonical.message = _db_growth_message(
                size_mb, band, crit_mb if severity == "critical" else warn_mb
            )
            canonical.evidence = _db_growth_evidence(
                size_mb, warn_mb, crit_mb, severity,
                first_observed_at=report["condition_first_observed_at"],
            )
        for alert in to_resolve:
            alert.status = ALERT_STATUS_RESOLVED
            alert.resolved_at = resolved_at
        session.commit()
    except Exception:
        session.rollback()
        raise

    report["persisted"] = True
    report["resolved"] = len(to_resolve)
    return report


def database_size_mb(settings: Settings | None = None) -> float | None:
    """Best-effort DB size (SQLite file only); None when unavailable."""
    import os

    from sqlalchemy.engine.url import make_url

    settings = settings or get_settings()
    try:
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite" and url.database and os.path.exists(url.database):
            return os.path.getsize(url.database) / (1024 * 1024)
    except Exception:  # pragma: no cover - defensive
        logger.debug("database_size_mb unavailable", exc_info=True)
    return None


class MarketOpsReportService:
    """Aggregate MarketOps view + a deterministic recommended operator action."""

    def build(self, session: Session, recent_limit: int = 10):
        from app.schemas import MarketOpsAlertOut, MarketOpsReport, MarketOpsRunOut
        from app.services.baseball_research import build_research_canary_report

        latest_run = session.execute(
            select(MarketOpsRun).order_by(MarketOpsRun.id.desc())
        ).scalars().first()
        runs_total = session.execute(
            select(func.count()).select_from(MarketOpsRun)
        ).scalar() or 0

        open_alerts = MarketOpsAlertService().list_recent(
            session, limit=recent_limit, status=ALERT_STATUS_OPEN
        )

        canary = build_research_canary_report(session)
        source_backed_packets = sum(
            stats.by_evidence_depth.get("source_backed", 0)
            for name, stats in canary.by_collector.items()
            if name.endswith("-external")
        )

        crypto_totals = {
            "tokens": session.execute(
                select(func.count()).select_from(CryptoToken)
            ).scalar() or 0,
            "signals": session.execute(
                select(func.count()).select_from(CryptoOpportunitySignal)
            ).scalar() or 0,
        }

        cc_snapshot = (
            (latest_run.summary or {}).get("champion_challenger") if latest_run else None
        )
        # SQLITE-BACKUP-FRESHNESS-ALERT-001: surfaced from the latest RUN
        # SUMMARY, not from open_alerts. open_alerts is `ORDER BY id DESC LIMIT
        # 10`, and db_growth_warning currently mints a new open row roughly
        # every 13 minutes (its title embeds a changing size, defeating the
        # (type, title) dedup), so a backup-protection alert would drop off
        # this report within ~2 hours and never come back. A per-run summary
        # key cannot be buried by another alert type.
        backup_freshness = (
            (latest_run.summary or {}).get("backup_freshness") if latest_run else None
        )
        # DB-GROWTH-ALERT-IDENTITY-001: surfaced from the run summary for the
        # same reason as backup_freshness. Once the backlog is reconciled to one
        # canonical row, that row carries one of the LOWEST ids in the table, so
        # `ORDER BY id DESC LIMIT 10` can never show it again while thousands of
        # info-level alerts keep accruing higher ids. Without this the repair
        # would make a 4.4 GB critical condition LESS visible than the bug it
        # replaced, and recommended_action would read "No action needed".
        db_growth = (
            (latest_run.summary or {}).get("db_growth") if latest_run else None
        )

        recommended = self._recommend(
            latest_run, open_alerts, cc_snapshot, backup_freshness, db_growth
        )

        return MarketOpsReport(
            runs_total=runs_total,
            latest_run=MarketOpsRunOut.model_validate(latest_run) if latest_run else None,
            open_alerts=[MarketOpsAlertOut.model_validate(a) for a in open_alerts],
            source_backed_packets=source_backed_packets,
            forecasts_by_forecaster=canary.forecasts_by_forecaster,
            champion_challenger=cc_snapshot,
            backup_freshness=backup_freshness,
            db_growth=db_growth,
            crypto_totals=crypto_totals,
            database_size_mb=(
                round(size, 2) if (size := database_size_mb()) is not None else None
            ),
            recommended_action=recommended,
        )

    @staticmethod
    def _recommend(
        latest_run, open_alerts, cc_snapshot, backup_freshness=None, db_growth=None
    ) -> str:
        # Backup protection first, and NAMED — ahead of the generic open-alert
        # count, which is permanently pinned by the db_growth_warning backlog
        # and would otherwise render this signal invisible.
        if backup_freshness is not None and not backup_freshness.get("healthy", True):
            return (
                "Backup protection is unhealthy "
                f"(reason={backup_freshness.get('reason')}, "
                f"age_seconds={backup_freshness.get('age_seconds')}) — "
                "run sqlite-backup-freshness-report and check the backup timer"
            )
        # Database growth, NAMED — ahead of the generic open-alert count, which
        # is capped at the 10 newest open rows and is therefore dominated by
        # info-level alerts.
        if db_growth is not None and db_growth.get("status") in ("warning", "critical"):
            return (
                f"Database is {db_growth.get('size_mb')} MiB, above the "
                f"{db_growth.get('status')} gate of "
                f"{db_growth.get('threshold_mb', db_growth.get('critical_mb'))} MiB "
                "— run db-growth-report and review retention coverage"
            )
        urgent = [a for a in open_alerts if a.severity in ("warning", "critical")]
        if urgent:
            return (
                f"Investigate {len(urgent)} open warning/critical alert(s) "
                "(marketops-alerts), then resolve them (marketops-resolve-alert <id>)"
            )
        if latest_run is None:
            return "Run `marketops-run-once` to record the first coordination cycle"
        if latest_run.status == "error":
            return f"Inspect last run error: {latest_run.error_type}"
        if latest_run.status == "partial":
            return "Review stage_errors in the last run summary"
        if cc_snapshot and cc_snapshot.get("sample_label") in (
            "insufficient_sample",
            "early_signal",
        ):
            return (
                "No action needed — keep accumulating paired outcomes before "
                "reading anything into champion/challenger deltas"
            )
        return "No action needed"
