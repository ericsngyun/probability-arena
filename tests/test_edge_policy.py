"""Edge shadow-policy analysis (EDGE-POLICY-001) tests: policy filtering,
exclusion/conservative predicates, per-policy follow-through, settlement Brier
comparison, the label ladder, and the decision/gate output. Read-only shadow
analysis — no live network, no trade semantics, no live-behavior change."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.edge_policy import (
    POLICY_PROMISING,
    POLICY_TOO_THIN,
    EdgePolicyReportService,
)

NOW = datetime.now(timezone.utc)
VALID = ("watchlist", "paper_candidate_later", "no_gap")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_snap(
    session,
    ticker,
    *,
    status="watchlist",
    gap=0.10,
    midpoint=0.50,
    persistence=1,
    minutes_ago=50,
    toward=None,
    conf=0.60,
    spread=3,
    liquidity=200_000,
    market_type="total",
    domain="sports_baseball",
    signal_type="price_move_threshold",
    phase_tags=None,
    resolved=None,
):
    f = MarketForecastRecord(
        market_ticker=ticker,
        forecaster_name="baseball_evidence",
        forecaster_version="v1",
        prompt_version="v1",
        estimated_probability=(midpoint + gap) if gap is not None else 0.5,
        confidence=conf,
        evidence_depth="source_backed",
        forecast_risk="medium",
        calibration_tags=phase_tags,
        created_at=NOW - timedelta(minutes=minutes_ago + 1),
    )
    session.add(f)
    session.commit()

    sig = OpportunitySignal(
        market_ticker=ticker,
        signal_type=signal_type,
        signal_status="forecast_refreshed",
        observed_at=NOW - timedelta(minutes=minutes_ago + 2),
        reason="seeded",
        created_at=NOW - timedelta(minutes=minutes_ago + 2),
    )
    session.add(sig)
    session.commit()

    row = EdgePrecheckSnapshot(
        market_ticker=ticker,
        signal_id=sig.id,
        forecast_id=f.id,
        forecaster_name="baseball_evidence",
        evidence_depth="source_backed",
        forecast_probability=(midpoint + gap) if gap is not None else 0.5,
        forecast_confidence=conf,
        market_midpoint=midpoint,
        spread_cents=spread,
        liquidity_proxy_cents=liquidity,
        probability_gap=gap,
        abs_probability_gap=abs(gap) if gap is not None else None,
        status=status,
        invalidation_reasons=[] if status in VALID else [status],
        persistence_count=persistence,
        tags=[f"domain:{domain}", f"market_type:{market_type}"],
        created_at=NOW - timedelta(minutes=minutes_ago),
    )
    session.add(row)
    session.commit()

    if toward is not None and gap is not None:
        direction = 1 if gap >= 0 else -1
        later_mid = midpoint + (0.04 if toward else -0.04) * direction
        session.add(
            MarketPriceTick(
                market_ticker=ticker,
                observed_at=NOW - timedelta(minutes=minutes_ago - 5),
                yes_bid=int(later_mid * 100) - 1,
                yes_ask=int(later_mid * 100) + 1,
                midpoint=later_mid,
                spread=spread,
                volume_24h=100,
                liquidity_proxy=liquidity,
                created_at=NOW - timedelta(minutes=minutes_ago - 5),
            )
        )
        session.commit()

    if resolved is not None:
        session.add(
            MarketOutcomeRecord(
                market_ticker=ticker,
                outcome_status="settled",
                resolved_probability=resolved,
                winning_side="yes" if resolved == 1.0 else "no",
                settlement_price=resolved,
                settled_time=NOW - timedelta(minutes=minutes_ago - 10),
                created_at=NOW,
            )
        )
        session.commit()
    return row


def seed_many(session, n, *, prefix, toward_count, **kwargs):
    for i in range(n):
        seed_snap(session, f"{prefix}-{i}", toward=(i < toward_count), **kwargs)


def service() -> EdgePolicyReportService:
    return EdgePolicyReportService()


def policy(report, name: str) -> dict:
    for p in report.policies:
        if p["name"] == name:
            return p
    raise AssertionError(f"policy {name!r} not found")


class TestPolicyFiltering:
    def test_baseline_includes_all_watchlist(self, session):
        seed_many(session, 4, prefix="T", toward_count=4, market_type="total")
        seed_many(session, 3, prefix="W", toward_count=3, market_type="winner")
        report = service().build(session)
        assert policy(report, "baseline_all_watchlist")["included"] == 7

    def test_exclude_winner_removes_winner_rows(self, session):
        seed_many(session, 4, prefix="T", toward_count=4, market_type="total")
        seed_many(session, 3, prefix="W", toward_count=3, market_type="winner")
        report = service().build(session)
        excl = policy(report, "exclude_winner")
        assert excl["included"] == 4
        assert "winner" not in excl["market_type_dist"]

    def test_totals_only_keeps_only_totals(self, session):
        seed_many(session, 4, prefix="T", toward_count=4, market_type="total")
        seed_many(session, 3, prefix="S", toward_count=3, market_type="spread")
        report = service().build(session)
        tot = policy(report, "totals_only")
        assert tot["included"] == 4
        assert set(tot["market_type_dist"]) == {"total"}

    def test_exclude_late_game_uses_forecast_phase(self, session):
        seed_many(session, 3, prefix="E", toward_count=3, phase_tags=["early_game"])
        seed_many(session, 2, prefix="L", toward_count=2, phase_tags=["late_game"])
        report = service().build(session)
        assert policy(report, "exclude_late_game")["included"] == 3

    def test_conservative_policy_applies_all_constraints(self, session):
        # Passes every constraint: spread/total, small gap, spread 2-5c, not winner/late.
        seed_snap(session, "GOOD", market_type="total", gap=0.08, spread=3,
                  liquidity=500_000, conf=0.60, phase_tags=["early_game"], toward=True)
        # Fails: winner
        seed_snap(session, "BADW", market_type="winner", gap=0.08, spread=3, toward=True)
        # Fails: big gap
        seed_snap(session, "BADG", market_type="total", gap=0.20, spread=3, toward=True)
        # Fails: neither spread 2-5c nor liquidity <100k (spread 1, big liquidity)
        seed_snap(session, "BADS", market_type="total", gap=0.08, spread=1,
                  liquidity=2_000_000, toward=True)
        report = service().build(session)
        cons = policy(report, "conservative_candidate_policy")
        assert cons["included"] == 1
        assert cons["market_type_dist"] == {"total": 1}


class TestFollowThrough:
    def test_moved_toward_rate_computes(self, session):
        seed_many(session, 4, prefix="T", toward_count=3, market_type="total")
        base = policy(service().build(session), "baseline_all_watchlist")
        # 3 of 4 toward => 0.75 at every horizon
        assert base["follow_through"]["15m"]["moved_toward_rate"] == 0.75
        assert base["blended_toward_rate"] == 0.75

    def test_invalid_rows_counted_but_no_follow_through(self, session):
        seed_snap(session, "INV", status="invalid_wide_spread", gap=None,
                  market_type="total", spread=40)
        base = policy(service().build(session), "baseline_all_watchlist")
        assert base["included"] == 1
        assert base["invalid"] == 1
        assert base["invalid_rate"] == 1.0
        assert base["follow_samples"] == 0


class TestSettlement:
    def test_brier_comparison_when_outcomes_exist(self, session):
        # forecast 0.70, market 0.50, outcome YES(1.0): forecast beats market
        seed_snap(session, "R1", midpoint=0.50, gap=0.20, market_type="total",
                  toward=True, resolved=1.0)
        report = service().build(session)
        assert report.settlement_available is True
        s = policy(report, "baseline_all_watchlist")["settlement"]
        assert s["resolved_samples"] == 1
        assert s["forecast_brier"] == pytest.approx(0.09, abs=1e-6)
        assert s["market_midpoint_brier"] == pytest.approx(0.25, abs=1e-6)
        assert s["forecast_minus_market_brier"] == pytest.approx(-0.16, abs=1e-6)
        assert s["forecast_beats_market_rate"] == 1.0

    def test_no_outcomes_means_settlement_unavailable(self, session):
        seed_snap(session, "T1", market_type="total", toward=True)
        report = service().build(session)
        assert report.settlement_available is False
        assert policy(report, "baseline_all_watchlist")["settlement"]["resolved_samples"] == 0


class TestLabelsAndGate:
    def test_thin_policy_labeled_too_thin(self, session):
        seed_many(session, 3, prefix="T", toward_count=3, market_type="total")
        report = service().build(session)
        # totals_only keeps 3 rows -> below the follow-sample floor
        assert policy(report, "totals_only")["recommendation"] == POLICY_TOO_THIN
        assert report.mvp_005b_blocked is True

    def test_weak_population_has_no_promising_policy(self, session):
        # 30 rows, only ~40% move toward -> every policy weak/neutral, none promising
        seed_many(session, 30, prefix="T", toward_count=12, market_type="total")
        report = service().build(session)
        assert all(p["recommendation"] != POLICY_PROMISING for p in report.policies)
        assert report.mvp_005b_blocked is True
        assert report.any_clears_follow_gate == []

    def test_exclude_winner_can_surface_promising_shadow(self, session):
        # 20 totals all toward (rate 1.0) + 20 winners all away (rate 0.0):
        # baseline blended 0.5; exclude_winner keeps the strong half.
        seed_many(session, 20, prefix="T", toward_count=20, market_type="total")
        seed_many(session, 20, prefix="W", toward_count=0, market_type="winner")
        report = service().build(session)
        excl = policy(report, "exclude_winner")
        assert excl["follow_samples"] >= 20
        assert excl["blended_toward_rate"] == 1.0
        assert excl["recommendation"] == POLICY_PROMISING
        assert any("exclude_winner" in c for c in report.any_clears_follow_gate)
        # Even when a shadow policy clears the gate, it only lifts the block as a
        # measurement signal — it never unlocks capability itself.
        assert report.mvp_005b_blocked is False
        assert "explicit human acceptance" in report.mvp_005b_reason


def test_main_wires_edge_policy_report(monkeypatch):
    captured = {}

    async def fake_report(hours=24, session=None):
        captured["hours"] = hours
        return 0

    monkeypatch.setattr(cli, "edge_policy_report", fake_report)
    assert cli.main(["edge-policy-report", "--hours", "48"]) == 0
    assert captured["hours"] == 48


def test_cli_prints_report(session, capsys):
    import asyncio

    seed_many(session, 6, prefix="T", toward_count=2, market_type="total")
    asyncio.run(cli.edge_policy_report(hours=24, session=session))
    out = capsys.readouterr().out
    assert "edge shadow-policy analysis" in out
    assert "policy: baseline_all_watchlist" in out
    assert "policy: conservative_candidate_policy" in out
    assert "MVP-005B-design gate" in out
    assert "blocked: True" in out
