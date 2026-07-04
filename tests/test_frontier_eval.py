"""Frontier evaluation harness (EVAL-001) tests: every quality section,
gap follow-through, the conservative readiness ladder, persistence, CLI, and
API. Evaluation only — no live network, no trade semantics anywhere."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import cli
from app.db import Base, get_db
from app.main import app
from app.models import (
    EdgePrecheckSnapshot,
    FrontierEvalRun,
    MarketForecastRecord,
    MarketOpsRun,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.frontier_eval import (
    MARKETOPS_P90_MAX_SECONDS,
    MIN_WATCHLIST_SAMPLE,
    READY_CYCLE_AUTOMATION,
    READY_NOT,
    READY_OBSERVE,
    EvalWindow,
    FrontierEvalService,
)

NOW = datetime.now(timezone.utc)
WINDOW = EvalWindow(start=NOW - timedelta(hours=24), end=NOW, hours=24)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_signal(session, ticker="KXMLB-A", promoted=True, processed=True, minutes_ago=60):
    observed = NOW - timedelta(minutes=minutes_ago)
    row = OpportunitySignal(
        market_ticker=ticker,
        signal_type="price_move_threshold",
        signal_status="forecast_refreshed" if processed else "new",
        observed_at=observed,
        promoted_at=observed + timedelta(seconds=30) if promoted else None,
        processed_at=observed + timedelta(seconds=90) if processed else None,
        reason="seeded",
        created_at=observed,
    )
    session.add(row)
    session.commit()
    return row


def seed_forecast(
    session, ticker="KXMLB-A", forecaster="baseball_evidence",
    depth="source_backed", minutes_ago=55,
):
    row = MarketForecastRecord(
        market_ticker=ticker,
        forecaster_name=forecaster,
        forecaster_version="v1",
        prompt_version="v1",
        estimated_probability=0.6,
        confidence=0.65,
        evidence_depth=depth,
        forecast_risk="medium",
        calibration_tags=["market_type_total"],
        created_at=NOW - timedelta(minutes=minutes_ago),
    )
    session.add(row)
    session.commit()
    return row


def seed_edge(
    session, ticker="KXMLB-A", status="watchlist", gap=0.10, midpoint=0.50,
    persistence=1, reasons=None, minutes_ago=50, forecast=None,
):
    forecast = forecast or seed_forecast(session, ticker=ticker)
    row = EdgePrecheckSnapshot(
        market_ticker=ticker,
        forecast_id=forecast.id,
        forecaster_name=forecast.forecaster_name,
        evidence_depth=forecast.evidence_depth,
        forecast_probability=midpoint + gap if gap is not None else 0.5,
        forecast_confidence=0.65,
        market_midpoint=midpoint,
        probability_gap=gap,
        abs_probability_gap=abs(gap) if gap is not None else None,
        status=status,
        invalidation_reasons=reasons if reasons is not None else (
            [] if status in ("watchlist", "paper_candidate_later", "no_gap") else [status]
        ),
        persistence_count=persistence,
        created_at=NOW - timedelta(minutes=minutes_ago),
    )
    session.add(row)
    session.commit()
    return row


def seed_tick(session, ticker="KXMLB-A", midpoint=0.5, minutes_ago=45, spread=4, liquidity=2000):
    observed = NOW - timedelta(minutes=minutes_ago)
    row = MarketPriceTick(
        market_ticker=ticker,
        observed_at=observed,
        yes_bid=int(midpoint * 100) - spread // 2 if midpoint is not None else None,
        yes_ask=int(midpoint * 100) + spread // 2 if midpoint is not None else None,
        midpoint=midpoint,
        spread=spread if midpoint is not None else None,
        volume_24h=100,
        liquidity_proxy=liquidity,
        created_at=observed,
    )
    session.add(row)
    session.commit()
    return row


def seed_marketops_run(session, duration_s=40.0, minutes_ago=30):
    started = NOW - timedelta(minutes=minutes_ago)
    row = MarketOpsRun(
        status="ok",
        started_at=started,
        finished_at=started + timedelta(seconds=duration_s),
        duration_ms=int(duration_s * 1000),
        created_at=started,
    )
    session.add(row)
    session.commit()
    return row


def service() -> FrontierEvalService:
    return FrontierEvalService()


class TestSignalQuality:
    def test_counts_and_rates(self, session):
        seed_signal(session, ticker="KXMLB-A")
        seed_signal(session, ticker="KXMLB-B", promoted=False, processed=False)
        seed_forecast(session, ticker="KXMLB-A")
        seed_forecast(session, ticker="KXMLB-B", depth="template_only")
        seed_edge(session, ticker="KXMLB-A", status="watchlist")

        quality = service().signal_quality(session, WINDOW, None)
        assert quality["signals_seen"] == 2
        assert quality["promoted"] == 1 and quality["promoted_rate"] == 0.5
        assert quality["processed"] == 1
        assert quality["forecasts"] == 3  # incl. the edge helper's forecast
        assert quality["source_backed_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert quality["watchlist_rate"] == 1.0
        assert quality["by_signal_type"]["price_move_threshold"] == 2
        assert quality["by_domain"]["sports_baseball"] == 2

    def test_domain_filter(self, session):
        seed_signal(session, ticker="KXMLB-A")
        seed_signal(session, ticker="KXWC-B")
        quality = service().signal_quality(session, WINDOW, ["sports_soccer"])
        assert quality["signals_seen"] == 1
        assert quality["by_domain"] == {"sports_soccer": 1}


class TestForecastQuality:
    def test_breakdowns(self, session):
        seed_forecast(session, forecaster="baseball_evidence")
        seed_forecast(session, forecaster="template_baseline", depth="template_only")
        quality = service().forecast_quality(session, WINDOW, None)
        assert quality["forecasts_in_window"] == 2
        assert quality["by_forecaster"] == {
            "baseball_evidence": 1, "template_baseline": 1
        }
        assert quality["by_market_type"] == {"total": 2}
        assert "0.6" in quality["by_confidence_bucket"]
        assert "sports_baseball" in quality["champion_challenger"]
        assert quality["champion_challenger"]["sports_baseball"]["paired_n"] == 0


class TestEdgeQuality:
    def test_status_reasons_persistence_direction(self, session):
        seed_edge(session, status="watchlist", gap=0.10, persistence=1)
        seed_edge(session, status="watchlist", gap=-0.08, persistence=2)
        seed_edge(session, status="paper_candidate_later", gap=0.12, persistence=3)
        seed_edge(session, status="no_gap", gap=0.01)
        seed_edge(
            session, status="invalid_wide_spread", gap=None,
            reasons=["invalid_wide_spread", "invalid_low_liquidity"],
        )
        quality = service().edge_quality(session, WINDOW, None)
        assert quality["total_snapshots"] == 5
        assert quality["watchlist"] == 2
        assert quality["paper_candidate_later"] == 1
        assert quality["valid_measurement_rate"] == pytest.approx(0.8, abs=1e-3)
        assert quality["invalid_explainable_rate"] == 1.0
        assert quality["gap_direction"] == {"positive": 3, "negative": 1}
        assert quality["persistence_distribution"] == {"1": 3, "2": 1, "3+": 1}
        assert quality["invalidation_reasons"]["invalid_low_liquidity"] == 1
        assert quality["mean_abs_gap"] == pytest.approx((0.1 + 0.08 + 0.12 + 0.01) / 4, abs=1e-3)

    def test_unexplained_invalid_detected(self, session):
        seed_edge(session, status="invalid_wide_spread", gap=None, reasons=[])
        quality = service().edge_quality(session, WINDOW, None)
        assert quality["invalid_explainable_rate"] == 0.0


class TestGapFollowThrough:
    def test_movement_toward_forecast(self, session):
        # watchlist: gap +0.10 from midpoint 0.50 (forecast 0.60)
        seed_edge(session, status="watchlist", gap=0.10, midpoint=0.50, minutes_ago=50)
        # later ticks: 10 min after snapshot midpoint moved to 0.54 (toward)
        seed_tick(session, midpoint=0.54, minutes_ago=40)
        follow = service().gap_follow_through(session, WINDOW, None)
        assert follow["watchlist_rows_analyzed"] == 1
        h15 = follow["horizons"]["15m"]
        assert h15["samples"] == 1
        assert h15["moved_toward_forecast"] == 1
        assert h15["moved_toward_rate"] == 1.0
        assert h15["mean_midpoint_delta"] == pytest.approx(0.04)
        assert h15["mean_gap_closure_pct"] == pytest.approx(0.4)
        # 5m horizon has no tick inside it
        assert follow["horizons"]["5m"]["samples"] == 0
        assert "not PnL" in follow["note"]

    def test_movement_away_counts_against(self, session):
        seed_edge(session, status="watchlist", gap=0.10, midpoint=0.50, minutes_ago=50)
        seed_tick(session, midpoint=0.45, minutes_ago=40)  # away from forecast
        follow = service().gap_follow_through(session, WINDOW, None)
        h15 = follow["horizons"]["15m"]
        assert h15["moved_toward_rate"] == 0.0
        assert h15["mean_gap_closure_pct"] == pytest.approx(-0.5)

    def test_invalid_rows_excluded(self, session):
        seed_edge(session, status="invalid_wide_spread", gap=None, reasons=["invalid_wide_spread"])
        follow = service().gap_follow_through(session, WINDOW, None)
        assert follow["watchlist_rows_analyzed"] == 0


class TestMicrostructure:
    def test_rates_and_percentiles(self, session):
        seed_tick(session, midpoint=0.5, spread=4, liquidity=2000)
        seed_tick(session, ticker="KXMLB-B", midpoint=None, spread=None, liquidity=0)
        seed_tick(session, ticker="KXWC-C", midpoint=0.4, spread=8, liquidity=800)
        quality = service().microstructure_quality(session, WINDOW, None)
        assert quality["ticks"] == 3
        assert quality["two_sided_rate"] == pytest.approx(2 / 3, abs=1e-3)
        assert quality["spread_cents_p50"] in (4.0, 8.0)
        assert quality["by_domain"]["sports_baseball"]["ticks"] == 2
        assert quality["by_domain"]["sports_soccer"]["two_sided_rate"] == 1.0


class TestCryptoQuality:
    def test_counts_and_post_signal_movement(self, session):
        from app.models import CryptoOpportunitySignal, CryptoPriceTick, CryptoToken

        session.add(CryptoToken(
            chain="solana", token_address="TOK", first_seen_at=NOW,
            last_seen_at=NOW, created_at=NOW,
        ))
        base_time = NOW - timedelta(minutes=30)
        session.add(CryptoPriceTick(
            chain="solana", token_address="TOK", pair_address="PAIR",
            observed_at=base_time, liquidity_usd=10_000.0, created_at=base_time,
        ))
        session.add(CryptoOpportunitySignal(
            chain="solana", token_address="TOK", pair_address="PAIR",
            signal_type="liquidity_removed", signal_status="new",
            observed_at=NOW - timedelta(minutes=20), reason="seeded",
            created_at=NOW - timedelta(minutes=20),
        ))
        later = NOW - timedelta(minutes=10)
        session.add(CryptoPriceTick(
            chain="solana", token_address="TOK", pair_address="PAIR",
            observed_at=later, liquidity_usd=2_000.0, created_at=later,
        ))
        session.commit()

        quality = service().crypto_quality(session, WINDOW)
        assert quality["tokens_seen"] == 1
        assert quality["liquidity_removed_signals"] == 1
        assert quality["post_risk_signal_samples"] == 1
        assert quality["post_risk_signal_liquidity_change_pct_mean"] == pytest.approx(-0.8)


class TestLatency:
    def test_percentiles_and_lags(self, session):
        for duration in (30, 40, 50):
            seed_marketops_run(session, duration_s=duration)
        signal = seed_signal(session)
        forecast = seed_forecast(session)
        signal.refreshed_forecast_id = forecast.id
        session.commit()
        seed_edge(session, forecast=forecast, minutes_ago=50)
        seed_tick(session, minutes_ago=1)

        quality = service().latency_quality(session, WINDOW)
        assert quality["marketops_runs"] == 3
        assert quality["marketops_duration_s_p50"] == 40.0
        assert quality["marketops_duration_s_p90"] == 50.0
        assert quality["signal_age_at_promotion_s_p50"] == 30.0
        assert quality["promotion_to_processed_s_p50"] == 60.0
        assert quality["signal_to_forecast_s_p50"] is not None
        assert quality["forecast_to_edge_precheck_s_p50"] is not None
        assert quality["latest_watcher_tick_age_s"] == pytest.approx(60, abs=5)


class TestSafetyAudit:
    def test_scan_is_clean_on_this_codebase(self):
        audit = service().safety_audit()
        assert audit["safety_ok"], audit["violations"]
        assert audit["files_scanned"] > 30
        assert "wallet" in audit["banned_identifier_fragments"]


class TestReadinessScorecard:
    def _report(self, session, **kwargs):
        return service().build(session, include_crypto=False, include_safety=False, **kwargs)

    def test_not_ready_without_watchlist_rows(self, session):
        seed_edge(session, status="invalid_wide_spread", gap=None,
                  reasons=["invalid_wide_spread"])
        report = self._report(session)
        assert report.readiness["label"] == READY_NOT
        assert "no valid watchlist rows" in report.readiness["reasons"][0]

    def test_observe_more_when_sample_thin(self, session):
        seed_marketops_run(session, duration_s=40)
        for i in range(3):
            seed_edge(session, ticker=f"KXMLB-{i}", status="watchlist", gap=0.1)
        report = self._report(session)
        assert report.readiness["label"] == READY_OBSERVE

    def test_cycle_automation_when_rules_satisfied(self, session):
        for duration in (30, 35, 40):
            seed_marketops_run(session, duration_s=duration)
        for i in range(MIN_WATCHLIST_SAMPLE):
            seed_edge(session, ticker=f"KXMLB-{i}", status="watchlist", gap=0.1)
        seed_edge(session, status="invalid_wide_spread", gap=None,
                  reasons=["invalid_wide_spread"])  # explainable invalid
        report = self._report(session)
        assert report.readiness["label"] == READY_CYCLE_AUTOMATION
        assert "MARKETOPS_INCLUDE_EDGE_PRECHECK" in report.recommended_next_action

    def test_manual_when_latency_too_slow(self, session):
        seed_marketops_run(session, duration_s=MARKETOPS_P90_MAX_SECONDS + 30)
        for i in range(MIN_WATCHLIST_SAMPLE):
            seed_edge(session, ticker=f"KXMLB-{i}", status="watchlist", gap=0.1)
        report = self._report(session)
        assert report.readiness["label"] == "ready_for_manual_edge_measurement"

    def test_no_live_or_autonomous_labels_exist(self):
        import app.services.frontier_eval as module

        source = open(module.__file__).read().lower()
        assert "ready_for_live" not in source
        assert "ready_for_autonomous" not in source
        assert "never authorize live capital" in source or "authorizes live capital" in source


class TestPersistence:
    def test_save_run_persists_summary(self, session):
        seed_edge(session, status="watchlist", gap=0.1)
        svc = service()
        report = svc.build(session, include_crypto=False, include_safety=False)
        row = svc.persist_run(session, report, NOW, 24)
        stored = session.execute(select(FrontierEvalRun)).scalars().one()
        assert stored.id == row.id
        assert stored.status == "ok"
        assert stored.summary["readiness"]["label"] == report.readiness["label"]
        assert stored.window_end is not None


class TestCliAndApi:
    async def test_cli_report_sections(self, session, capsys):
        seed_marketops_run(session)
        seed_edge(session, status="watchlist", gap=0.1)
        exit_code = await cli.frontier_eval_report(
            hours=24, include_crypto=True, include_safety=True, session=session
        )
        assert exit_code == 0
        output = capsys.readouterr().out
        for header in (
            "executive summary", "readiness scorecard", "signal quality",
            "forecast quality", "edge-precheck quality",
            "gap follow-through (market movement, not PnL)",
            "microstructure quality", "crypto risk quality", "latency quality",
            "safety audit", "recommended next action",
        ):
            assert header in output
        assert "evaluation only" in output

    async def test_cli_save_run(self, session, capsys):
        seed_edge(session, status="watchlist", gap=0.1)
        await cli.frontier_eval_report(session=session, save_run=True)
        output = capsys.readouterr().out
        assert "saved eval run #1" in output
        assert session.execute(select(FrontierEvalRun)).scalars().one() is not None

    def test_main_wires_command(self, monkeypatch):
        captured = []

        async def fake(*args, **kwargs):
            captured.append(kwargs)
            return 0

        monkeypatch.setattr(cli, "frontier_eval_report", fake)
        assert cli.main([
            "frontier-eval-report", "--hours", "12", "--domain", "sports_baseball",
            "--include-crypto", "--include-safety", "--save-run",
        ]) == 0
        assert captured[0]["hours"] == 12
        assert captured[0]["domains"] == ["sports_baseball"]
        assert captured[0]["include_crypto"] is True
        assert captured[0]["save_run"] is True


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), session
    app.dependency_overrides.clear()


class TestApi:
    def test_frontier_report_endpoint(self, client):
        test_client, session = client
        seed_edge(session, status="watchlist", gap=0.1)
        seed_marketops_run(session)

        report = test_client.get(
            "/eval/frontier-report?hours=24&include_crypto=true&include_safety=false"
        ).json()
        assert report["window_hours"] == 24
        assert report["readiness"]["label"] in (
            READY_NOT, READY_OBSERVE, "ready_for_manual_edge_measurement",
            READY_CYCLE_AUTOMATION,
        )
        assert report["safety_audit"] is None
        assert report["crypto_risk_quality"] is not None
        assert "not PnL" in report["gap_follow_through"]["note"]
        assert "no label authorizes live capital" in report["readiness"]["note"]

        filtered = test_client.get(
            "/eval/frontier-report?domain=sports_soccer&include_crypto=false"
        ).json()
        assert filtered["domains"] == ["sports_soccer"]
        assert filtered["crypto_risk_quality"] is None
