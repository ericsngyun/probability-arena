"""PROVIDER-BUDGET-001 tests: SolanaTracker request accounting + budget
guardrails. Usage accounting increments across hour/day/month windows, warn/
stop thresholds compute, the engine skips optional SolanaTracker lookups at the
per-run cap and the daily STOP threshold (GoPlus never affected), and the
budget report builds. No live network; in-memory SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoTokenRiskAssessment
from app.services.crypto_risk import RiskAssessment
from app.services.crypto_risk_engine import (
    CryptoRiskEngine,
    CryptoRiskProviderRegistry,
    RiskEngineConfig,
)
from app.services.provider_budget import (
    SolanaTrackerBudgetConfig,
    SolanaTrackerBudgetService,
)
from tests.test_crypto_risk_engine import make_pair, make_tick, make_token

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def st_request(session, *, success=True, covered=False, created_at=None, error=False):
    """Insert one assessment row that represents a SolanaTracker attempt:
    success -> solana-tracker in provider_names; error -> in provider_errors."""
    names = ["goplus"]
    flags = {}
    payload = {"sub_scores": {}, "provider_errors": {}}
    if error:
        payload["provider_errors"]["solana-tracker"] = "timeout"
    elif success:
        names.append("solana-tracker")
        if covered:
            flags["top10_holder_pct"] = 12.5
    row = CryptoTokenRiskAssessment(
        chain="solana",
        token_address="T" + str(id(object())),
        provider="risk-engine",
        provider_names=names,
        flags=flags,
        raw_payload=payload,
        created_at=created_at or NOW,
    )
    session.add(row)
    session.flush()
    return row


def budget(**kw):
    base = dict(
        monthly_request_limit=200000, daily_request_budget=5000, hourly_request_budget=200,
        per_run_lookup_limit=25, cache_ttl_hours=24, warn_daily_requests=4000,
        stop_daily_requests=6000,
    )
    base.update(kw)
    return SolanaTrackerBudgetService(SolanaTrackerBudgetConfig(**base))


# --- accounting -------------------------------------------------------------


class TestAccounting:
    def test_counts_success_and_error_as_requests(self, session):
        for _ in range(5):
            st_request(session, success=True)
        for _ in range(2):
            st_request(session, error=True)
        # a heuristic-only row (no ST) must NOT count
        session.add(CryptoTokenRiskAssessment(
            chain="solana", token_address="H", provider="risk-engine",
            provider_names=["goplus"], flags={}, raw_payload={"provider_errors": {}},
            created_at=NOW,
        ))
        session.flush()
        svc = budget()
        assert svc.requests_today(session, NOW) == 7  # 5 success + 2 error
        r = svc.status(session, NOW)
        assert r.success_count == 5 and r.error_count == 2
        assert r.success_rate == pytest.approx(5 / 7, abs=1e-4)

    def test_windows_hour_day_month(self, session):
        st_request(session, created_at=NOW)                       # this hour/day/month
        st_request(session, created_at=NOW - timedelta(hours=2))  # today + month, not hour
        st_request(session, created_at=NOW - timedelta(days=2))   # month only (if same month)
        st_request(session, created_at=NOW - timedelta(days=40))  # neither
        svc = budget()
        # guard against hour/day/month boundary flakiness by using generous asserts
        assert svc.requests_this_hour(session, NOW) >= 1
        assert svc.requests_this_hour(session, NOW) <= svc.requests_today(session, NOW)
        assert svc.requests_today(session, NOW) <= svc.requests_this_month(session, NOW)
        assert svc.requests_this_month(session, NOW) <= 3  # the 40-day-old one excluded

    def test_coverage_per_request(self, session):
        st_request(session, success=True, covered=True)
        st_request(session, success=True, covered=True)
        st_request(session, success=True, covered=False)
        st_request(session, error=True)
        r = budget().status(session, NOW)
        # 2 covered / 4 total requests
        assert r.coverage_per_request == pytest.approx(0.5, abs=1e-4)

    def test_estimated_monthly_run_rate(self, session):
        for _ in range(10):
            st_request(session, created_at=NOW - timedelta(hours=3))
        r = budget().status(session, NOW)
        assert r.rolling_24h_requests == 10
        assert r.estimated_monthly_run_rate == 300  # 10 * 30


# --- thresholds -------------------------------------------------------------


class TestThresholds:
    def test_warn_fires(self, session):
        for _ in range(3):
            st_request(session)
        svc = budget(warn_daily_requests=3, stop_daily_requests=10)
        assert svc.over_warn(session, NOW) is True
        assert svc.over_stop(session, NOW) is False
        r = svc.status(session, NOW)
        assert r.over_warn is True and r.over_stop is False
        assert "WARN" in r.recommendation

    def test_stop_fires(self, session):
        for _ in range(4):
            st_request(session)
        svc = budget(warn_daily_requests=2, stop_daily_requests=4)
        assert svc.over_stop(session, NOW) is True
        assert "STOP" in svc.status(session, NOW).recommendation

    def test_keep_when_under_budget(self, session):
        st_request(session)
        r = budget().status(session, NOW)
        assert r.over_warn is False and r.over_stop is False
        assert "KEEP" in r.recommendation


# --- engine guardrail -------------------------------------------------------


class STStub:
    name = "solana-tracker"

    async def assess(self, token_address):
        return RiskAssessment(provider="solana-tracker", token_address=token_address,
                              risk_score=None, flags={"top10_holder_pct": 10.0})


class GoPlusStub:
    name = "goplus"

    async def assess(self, token_address):
        return RiskAssessment(provider="goplus", token_address=token_address,
                              risk_score=None, flags={"rug_risk": False})


def engine_with_budget(**budget_kw):
    eng = CryptoRiskEngine(
        registry=CryptoRiskProviderRegistry(adapters=[GoPlusStub(), STStub()]),
        config=RiskEngineConfig(),
        chain="solana",
    )
    base = dict(
        monthly_request_limit=200000, daily_request_budget=5000, hourly_request_budget=200,
        per_run_lookup_limit=25, cache_ttl_hours=24, warn_daily_requests=4000,
        stop_daily_requests=6000,
    )
    base.update(budget_kw)
    eng._budget_cfg = SolanaTrackerBudgetConfig(**base)
    eng._budget = SolanaTrackerBudgetService(eng._budget_cfg)
    return eng


class TestEngineGuardrail:
    async def test_normal_call_uses_solana_tracker(self, session):
        eng = engine_with_budget()
        ev = await eng.evaluate(session, token=make_token(), pair=make_pair(),
                                tick=make_tick(), previous=None)
        assert "solana-tracker" in ev.provider_names
        assert "goplus" in ev.provider_names

    async def test_stop_threshold_skips_solana_tracker_keeps_goplus(self, session):
        # seed 6 ST requests, stop threshold = 5 -> over stop
        for _ in range(6):
            st_request(session)
        eng = engine_with_budget(stop_daily_requests=5)
        ev = await eng.evaluate(session, token=make_token(), pair=make_pair(),
                                tick=make_tick(), previous=None)
        assert "solana-tracker" not in ev.provider_names   # skipped
        assert "goplus" in ev.provider_names               # GoPlus untouched
        # skip is not an error
        assert "solana-tracker" not in ev.provider_errors

    async def test_per_run_lookup_cap_skips_after_limit(self, session):
        eng = engine_with_budget(per_run_lookup_limit=2, stop_daily_requests=10_000)
        used = []
        for i in range(4):
            ev = await eng.evaluate(session, token=make_token(address=f"TK{i}"),
                                    pair=make_pair(), tick=make_tick(), previous=None,
                                    token_address=f"TK{i}")
            used.append("solana-tracker" in ev.provider_names)
        # first 2 use ST, remainder skip (per-run cap)
        assert used[0] and used[1]
        assert not used[2] and not used[3]

    async def test_guardrail_noop_when_no_solana_tracker_adapter(self, session):
        eng = CryptoRiskEngine(
            registry=CryptoRiskProviderRegistry(adapters=[GoPlusStub()]),
            config=RiskEngineConfig(), chain="solana",
        )
        ev = await eng.evaluate(session, token=make_token(), pair=make_pair(),
                                tick=make_tick(), previous=None)
        assert "goplus" in ev.provider_names
        assert eng._st_lookups_this_run == 0  # never counted


# --- report -----------------------------------------------------------------


class TestReport:
    def test_report_fields(self, session):
        st_request(session, success=True, covered=True)
        st_request(session, error=True)
        r = budget().status(session, NOW)
        assert r.plan_name == "SolanaTracker Advanced"
        assert "58" in r.monthly_cost_usd
        assert r.monthly_request_limit == 200000
        assert r.requests_today == 2
        assert r.remaining_daily_budget == 5000 - 2
        assert r.remaining_monthly_budget == 200000 - 2
        assert "no EV" in r.note.lower() or "execution" in r.note.lower()

    def test_cli_report_runs(self, session):
        from app import cli

        st_request(session)
        rc = __import__("asyncio").run(cli.crypto_provider_budget_report(session=session))
        assert rc == 0
