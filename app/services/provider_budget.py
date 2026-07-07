"""SolanaTracker request accounting + budget guardrails (PROVIDER-BUDGET-001).

Cost/usage OBSERVABILITY for the SolanaTracker Advanced plan (~$58-59/month
USD, 200,000 requests/month). Usage is DERIVED read-only from the existing
`crypto_token_risk_assessments` rows — each row that attempted SolanaTracker
carries `solana-tracker` in `provider_names` (success) or in
`raw_payload.provider_errors` (failure); a row where the budget guardrail
SKIPPED the call carries neither, so skips are correctly not counted as
requests. No new table, no migration.

The budget guardrail can only SKIP optional SolanaTracker lookups when over
budget (the token then falls back to GoPlus + heuristics — a fully supported
mode). It never adds calls, never changes GoPlus/Birdeye behavior, and attaches
no EV/trade/sizing/order/wallet/signing/execution semantics. This is provider
cost accounting, not trading logic.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import CryptoTokenRiskAssessment

PROVIDER = "solana-tracker"
PLAN_NAME = "SolanaTracker Advanced"
MONTHLY_COST_USD = "~$58-59"
# operational monthly target (under the hard plan ceiling) — advisory only
MONTHLY_TARGET_REQUESTS = 150000

# JSON-quoted token; matches the provider name inside the JSON text of either
# provider_names (["goplus", "solana-tracker"]) or raw_payload.provider_errors
# ({"solana-tracker": "..."}). SQLite stores JSON as TEXT; Postgres JSONB casts
# to text — both support this LIKE.
_TOKEN = f'%"{PROVIDER}"%'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _success_clause():
    return cast(CryptoTokenRiskAssessment.provider_names, String).like(_TOKEN)


def _error_clause():
    return cast(CryptoTokenRiskAssessment.raw_payload, String).like(_TOKEN)


def _covered_clause():
    # a successful ST read that carried top-holder concentration data
    return cast(CryptoTokenRiskAssessment.flags, String).like('%"top10_holder_pct"%')


@dataclass
class SolanaTrackerBudgetConfig:
    monthly_request_limit: int = 200000
    daily_request_budget: int = 5000
    hourly_request_budget: int = 200
    per_run_lookup_limit: int = 25
    cache_ttl_hours: int = 24
    warn_daily_requests: int = 4000
    stop_daily_requests: int = 6000

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "SolanaTrackerBudgetConfig":
        s = settings or get_settings()
        return cls(
            monthly_request_limit=s.solana_tracker_monthly_request_limit,
            daily_request_budget=s.solana_tracker_daily_request_budget,
            hourly_request_budget=s.solana_tracker_hourly_request_budget,
            per_run_lookup_limit=s.solana_tracker_per_run_lookup_limit,
            cache_ttl_hours=s.solana_tracker_cache_ttl_hours,
            warn_daily_requests=s.solana_tracker_warn_daily_requests,
            stop_daily_requests=s.solana_tracker_stop_daily_requests,
        )


@dataclass
class SolanaTrackerBudgetReport:
    note: str
    provider_enabled: bool
    plan_name: str
    monthly_cost_usd: str
    monthly_request_limit: int
    daily_budget: int
    hourly_budget: int
    per_run_lookup_limit: int
    cache_ttl_hours: int
    warn_daily: int
    stop_daily: int
    requests_this_hour: int
    requests_today: int
    requests_this_month: int
    rolling_24h_requests: int
    estimated_monthly_run_rate: int
    remaining_daily_budget: int
    remaining_monthly_budget: int
    success_count: int
    error_count: int
    success_rate: float | None
    coverage_per_request: float | None
    over_hourly: bool
    over_warn: bool
    over_stop: bool
    recommendation: str
    windows: dict = field(default_factory=dict)


class SolanaTrackerBudgetService:
    """Read-only windowed request accounting derived from persisted risk
    assessments, plus the warn/stop decisions the engine guardrail consults."""

    def __init__(self, config: SolanaTrackerBudgetConfig | None = None):
        self.config = config or SolanaTrackerBudgetConfig.from_settings()

    # --- primitive counts ---------------------------------------------------

    def _count(self, session: Session, *conditions) -> int:
        return session.execute(
            select(func.count()).select_from(CryptoTokenRiskAssessment).where(*conditions)
        ).scalar() or 0

    def _requests_since(self, session: Session, since: datetime) -> int:
        """SolanaTracker requests (success + error) since `since`. A row is in
        exactly one of the two clauses, so summing is exact."""
        ts = CryptoTokenRiskAssessment.created_at >= since
        return self._count(session, ts, _success_clause()) + self._count(session, ts, _error_clause())

    def requests_today(self, session: Session, now: datetime | None = None) -> int:
        now = now or _now()
        return self._requests_since(session, now.replace(hour=0, minute=0, second=0, microsecond=0))

    def requests_this_hour(self, session: Session, now: datetime | None = None) -> int:
        now = now or _now()
        return self._requests_since(session, now.replace(minute=0, second=0, microsecond=0))

    def requests_this_month(self, session: Session, now: datetime | None = None) -> int:
        now = now or _now()
        return self._requests_since(
            session, now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )

    # --- guardrail decisions ------------------------------------------------

    def over_stop(self, session: Session, now: datetime | None = None) -> bool:
        return self.requests_today(session, now) >= self.config.stop_daily_requests

    def over_warn(self, session: Session, now: datetime | None = None) -> bool:
        return self.requests_today(session, now) >= self.config.warn_daily_requests

    # --- full report --------------------------------------------------------

    def status(self, session: Session, now: datetime | None = None) -> SolanaTrackerBudgetReport:
        now = now or _now()
        cfg = self.config
        s = get_settings()

        req_hour = self.requests_this_hour(session, now)
        req_today = self.requests_today(session, now)
        req_month = self.requests_this_month(session, now)
        rolling_24h = self._requests_since(session, now - timedelta(hours=24))
        est_monthly = rolling_24h * 30

        success = self._count(session, _success_clause())
        error = self._count(session, _error_clause())
        requests_all = success + error
        covered = self._count(session, _success_clause(), _covered_clause())

        over_warn = req_today >= cfg.warn_daily_requests
        over_stop = req_today >= cfg.stop_daily_requests
        over_hourly = req_hour >= cfg.hourly_request_budget

        if over_stop:
            rec = (
                "STOP: daily requests at/above the stop threshold — optional SolanaTracker "
                "lookups are being SKIPPED this run (tokens fall back to GoPlus+heuristics). "
                "Investigate scan volume or lower SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT."
            )
        elif over_warn:
            rec = (
                "WARN: daily requests above the warning threshold — monitor; consider "
                "lowering SOLANA_TRACKER_PER_RUN_LOOKUP_LIMIT or scan cadence."
            )
        elif est_monthly > cfg.monthly_request_limit:
            rec = (
                "TUNE: 24h run-rate projects OVER the monthly plan limit "
                f"({est_monthly:,} > {cfg.monthly_request_limit:,}) — lower per-run limit/cadence."
            )
        elif est_monthly > MONTHLY_TARGET_REQUESTS:
            rec = (
                f"WATCH: run-rate {est_monthly:,}/mo is above the {MONTHLY_TARGET_REQUESTS:,} "
                "operational target but under the plan limit."
            )
        else:
            rec = "KEEP: SolanaTracker usage is well within the daily and monthly budget."

        return SolanaTrackerBudgetReport(
            note=(
                "Read-only provider cost/usage observability. SolanaTracker Advanced "
                f"{MONTHLY_COST_USD}/month. Budget guardrails only SKIP optional lookups "
                "when over budget (GoPlus/Birdeye unaffected); no EV, trade, sizing, order, "
                "wallet, signing, or execution."
            ),
            provider_enabled=bool(getattr(s, "enable_solana_tracker_risk", False)),
            plan_name=PLAN_NAME,
            monthly_cost_usd=MONTHLY_COST_USD,
            monthly_request_limit=cfg.monthly_request_limit,
            daily_budget=cfg.daily_request_budget,
            hourly_budget=cfg.hourly_request_budget,
            per_run_lookup_limit=cfg.per_run_lookup_limit,
            cache_ttl_hours=cfg.cache_ttl_hours,
            warn_daily=cfg.warn_daily_requests,
            stop_daily=cfg.stop_daily_requests,
            requests_this_hour=req_hour,
            requests_today=req_today,
            requests_this_month=req_month,
            rolling_24h_requests=rolling_24h,
            estimated_monthly_run_rate=est_monthly,
            remaining_daily_budget=max(0, cfg.daily_request_budget - req_today),
            remaining_monthly_budget=max(0, cfg.monthly_request_limit - req_month),
            success_count=success,
            error_count=error,
            success_rate=round(success / requests_all, 4) if requests_all else None,
            coverage_per_request=round(covered / requests_all, 4) if requests_all else None,
            over_hourly=over_hourly,
            over_warn=over_warn,
            over_stop=over_stop,
            recommendation=rec,
            windows={
                "hour_budget": cfg.hourly_request_budget,
                "day_budget": cfg.daily_request_budget,
                "over_hourly": over_hourly,
            },
        )
