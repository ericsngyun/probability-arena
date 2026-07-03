from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    ForecastScoreRecord,
    MarketForecastRecord,
    MarketOutcomeRecord,
    OpportunitySignal,
)
from app.services.champion_challenger import (
    ChampionChallengerService,
    confidence_bucket,
    sample_label,
)

NOW = datetime.now(timezone.utc)
SERVICE = ChampionChallengerService()


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def seed_outcome(session, ticker) -> MarketOutcomeRecord:
    row = MarketOutcomeRecord(
        market_ticker=ticker, outcome_status="settled", winning_side="yes",
        resolved_probability=1.0, source="kalshi_rest", created_at=NOW,
    )
    session.add(row)
    session.commit()
    return row


def seed_scored(
    session,
    ticker,
    forecaster="template_baseline",
    version="v1",
    brier=0.25,
    log_loss=0.69,
    abs_error=0.5,
    confidence=0.5,
    tags=None,
    score_status="scored",
    outcome=None,
    domain="sports_baseball",
    created_at=None,
    evidence_depth="template_only",
    risk="medium",
) -> MarketForecastRecord:
    forecast = MarketForecastRecord(
        market_ticker=ticker,
        forecaster_name=forecaster,
        forecaster_version=version,
        prompt_version="v1",
        estimated_probability=0.5,
        confidence=confidence,
        evidence_depth=evidence_depth,
        forecast_risk=risk,
        forecast_summary="seeded",
        calibration_tags=tags or [],
        created_at=created_at or NOW,
    )
    session.add(forecast)
    session.commit()
    score = ForecastScoreRecord(
        forecast_id=forecast.id,
        market_ticker=ticker,
        outcome_id=outcome.id if outcome else None,
        brier_score=brier if score_status == "scored" else None,
        log_loss=log_loss if score_status == "scored" else None,
        absolute_error=abs_error if score_status == "scored" else None,
        was_resolved=score_status == "scored",
        score_status=score_status,
        score_tags=[f"forecaster:{forecaster}", f"domain:{domain}"],
        created_at=created_at or NOW,
    )
    session.add(score)
    session.commit()
    return forecast


def seed_pair(session, ticker, base_brier, chal_brier, tags=None, confidence=0.6):
    """Baseline + challenger scored against the same outcome."""
    outcome = seed_outcome(session, ticker)
    seed_scored(session, ticker, forecaster="template_baseline",
                brier=base_brier, log_loss=base_brier * 2, abs_error=base_brier,
                outcome=outcome)
    return seed_scored(
        session, ticker, forecaster="baseball_evidence", version="v1",
        brier=chal_brier, log_loss=chal_brier * 2, abs_error=chal_brier,
        confidence=confidence, outcome=outcome, evidence_depth="source_backed",
        tags=tags or ["baseball_evidence_v1", "market_type_total", "late_game"],
    )


class TestHelpers:
    def test_sample_labels(self):
        assert sample_label(0) == "insufficient_sample"
        assert sample_label(29) == "insufficient_sample"
        assert sample_label(30) == "early_signal"
        assert sample_label(99) == "early_signal"
        assert sample_label(100) == "useful_sample"
        assert sample_label(299) == "useful_sample"
        assert sample_label(300) == "stronger_sample"

    def test_confidence_buckets(self):
        assert confidence_bucket(0.0) == "0.00-0.25"
        assert confidence_bucket(0.25) == "0.25-0.50"
        assert confidence_bucket(0.55) == "0.50-0.60"
        assert confidence_bucket(0.6) == "0.60-0.70"
        assert confidence_bucket(0.75) == "0.70-0.80"
        assert confidence_bucket(0.95) == "0.80-1.00"
        assert confidence_bucket(1.0) == "0.80-1.00"
        assert confidence_bucket(None) == "unknown"


class TestAggregates:
    def test_aggregate_metrics_and_deltas(self, session):
        seed_pair(session, "T-1", base_brier=0.30, chal_brier=0.10)
        seed_pair(session, "T-2", base_brier=0.20, chal_brier=0.20)

        summary = SERVICE.compare(session)
        assert summary.baseline.scored.count_scored == 2
        assert summary.challenger.scored.count_scored == 2
        assert summary.baseline.scored.mean_brier == pytest.approx(0.25)
        assert summary.challenger.scored.mean_brier == pytest.approx(0.15)
        assert summary.delta_brier == pytest.approx(-0.10)
        assert summary.delta_log_loss == pytest.approx(-0.20)
        assert summary.comparison_basis == "unpaired"

    def test_coverage_pending_unscorable_counts(self, session):
        outcome = seed_outcome(session, "T-3")
        seed_scored(session, "T-3", forecaster="template_baseline", outcome=outcome)
        seed_scored(session, "T-4", forecaster="template_baseline", score_status="pending_outcome")
        seed_scored(session, "T-5", forecaster="template_baseline", score_status="unscorable")

        summary = SERVICE.compare(session)
        assert summary.baseline.coverage == 3
        assert summary.baseline.scored.count_scored == 1
        assert summary.baseline.pending == 1
        assert summary.baseline.unscorable == 1
        assert summary.challenger.scored.count_scored == 0
        assert summary.challenger.scored.mean_brier is None

    def test_challenger_name_matches_name_version_composite(self, session):
        seed_pair(session, "T-6", base_brier=0.2, chal_brier=0.1)
        summary = SERVICE.compare(session, challenger="baseball_evidence_v1")
        assert summary.challenger.scored.count_scored == 1


class TestPairing:
    def test_paired_deltas_and_win_rate(self, session):
        seed_pair(session, "P-1", base_brier=0.30, chal_brier=0.10)  # win
        seed_pair(session, "P-2", base_brier=0.10, chal_brier=0.30)  # loss
        seed_pair(session, "P-3", base_brier=0.20, chal_brier=0.20)  # tie
        # challenger-only market: not a pair
        seed_scored(session, "P-4", forecaster="baseball_evidence",
                    outcome=seed_outcome(session, "P-4"))

        summary = SERVICE.compare(session)
        paired = summary.paired
        assert paired.pair_count == 3
        assert paired.wins == 1 and paired.losses == 1 and paired.ties == 1
        assert paired.win_rate_by_market == pytest.approx(1 / 3, abs=1e-4)
        assert paired.mean_delta_brier == pytest.approx(0.0)
        assert paired.sample_label == "insufficient_sample"

    def test_latest_scored_forecast_per_ticker_is_representative(self, session):
        outcome = seed_outcome(session, "P-5")
        seed_scored(session, "P-5", forecaster="baseball_evidence", brier=0.40, outcome=outcome)
        seed_scored(  # newer forecast for the same ticker: wins representation
            session, "P-5", forecaster="baseball_evidence", brier=0.10, outcome=outcome,
            created_at=NOW + timedelta(minutes=5),
        )
        seed_scored(session, "P-5", forecaster="template_baseline", brier=0.20, outcome=outcome)

        summary = SERVICE.compare(session)
        assert summary.challenger.scored.count_scored == 1
        assert summary.challenger.scored.mean_brier == pytest.approx(0.10)
        assert summary.paired.pair_count == 1
        assert summary.paired.wins == 1

    def test_paired_only_restricts_aggregates(self, session):
        seed_pair(session, "P-6", base_brier=0.30, chal_brier=0.10)
        # baseline-only ticker inflates unpaired baseline metrics
        seed_scored(session, "P-7", forecaster="template_baseline", brier=0.90,
                    outcome=seed_outcome(session, "P-7"))

        unpaired = SERVICE.compare(session)
        assert unpaired.baseline.scored.count_scored == 2
        paired = SERVICE.compare(session, paired_only=True)
        assert paired.comparison_basis == "paired"
        assert paired.baseline.scored.count_scored == 1
        assert paired.baseline.scored.mean_brier == pytest.approx(0.30)

    def test_no_pairs_is_reported_as_none(self, session):
        seed_scored(session, "P-8", forecaster="template_baseline",
                    outcome=seed_outcome(session, "P-8"))
        summary = SERVICE.compare(session)
        assert summary.paired is None


class TestCohortsAndFilters:
    def test_market_type_and_game_stage_cohorts(self, session):
        seed_pair(session, "C-1", 0.3, 0.1,
                  tags=["baseball_evidence_v1", "market_type_total", "late_game"])
        seed_pair(session, "C-2", 0.2, 0.3,
                  tags=["baseball_evidence_v1", "market_type_spread", "early_game"])

        summary = SERVICE.compare(session)
        market_types = {row.cohort: row for row in summary.by_market_type}
        # template forecasts carry no market_type tag -> 'unknown' cohort
        assert set(market_types) == {"total", "spread", "unknown"}
        assert market_types["total"].challenger.count_scored == 1
        assert market_types["total"].delta_brier is None  # baseline absent in cohort
        assert market_types["unknown"].baseline.count_scored == 2
        stages = {row.cohort for row in summary.by_game_stage}
        assert {"late_game", "early_game", "unknown"} == stages
        assert all(row.paired is False for row in summary.by_market_type)

    def test_confidence_bucket_cohorts(self, session):
        seed_pair(session, "C-3", 0.3, 0.1, confidence=0.65)
        seed_pair(session, "C-4", 0.3, 0.1, confidence=0.45)
        summary = SERVICE.compare(session)
        buckets = {row.cohort for row in summary.by_confidence_bucket}
        assert "0.60-0.70" in buckets
        assert "0.25-0.50" in buckets  # both template (0.5) -> 0.50-0.60 too
        assert "0.50-0.60" in buckets

    def test_signal_type_cohort_and_filter(self, session):
        forecast = seed_pair(session, "C-5", 0.3, 0.1)
        session.add(
            OpportunitySignal(
                market_ticker="C-5", signal_type="price_move_threshold",
                signal_status="forecast_refreshed", observed_at=NOW,
                reason="seeded", refreshed_forecast_id=forecast.id, created_at=NOW,
            )
        )
        session.commit()
        seed_pair(session, "C-6", 0.2, 0.2)  # no signal linkage

        summary = SERVICE.compare(session)
        signal_rows = {row.cohort: row for row in summary.by_signal_type}
        assert list(signal_rows) == ["price_move_threshold"]
        assert signal_rows["price_move_threshold"].challenger.count_scored == 1

        filtered = SERVICE.compare(session, signal_type="price_move_threshold")
        assert filtered.challenger.scored.count_scored == 1
        assert filtered.filters["signal_type"] == "price_move_threshold"

    def test_domain_filter(self, session):
        seed_pair(session, "C-7", 0.3, 0.1)
        summary = SERVICE.compare(session, domain="sports_tennis")
        assert summary.challenger.scored.count_scored == 0
        summary = SERVICE.compare(session, domain="sports_baseball")
        assert summary.challenger.scored.count_scored == 1

    def test_created_at_filters(self, session):
        seed_pair(session, "C-8", 0.3, 0.1)
        summary = SERVICE.compare(session, min_created_at=NOW + timedelta(days=1))
        assert summary.challenger.scored.count_scored == 0
        summary = SERVICE.compare(session, max_created_at=NOW + timedelta(days=1))
        assert summary.challenger.scored.count_scored == 1


class TestSampleInterpretation:
    def test_insufficient_sample_warning(self, session):
        seed_pair(session, "S-1", 0.3, 0.1)
        summary = SERVICE.compare(session)
        assert summary.sample_label == "insufficient_sample"
        assert "do NOT infer edge" in summary.warning

    def test_warning_clears_at_threshold(self, session):
        for i in range(30):
            seed_pair(session, f"S-N{i}", 0.3, 0.1)
        summary = SERVICE.compare(session)
        assert summary.sample_label == "early_signal"
        assert summary.warning is None

    def test_min_count_option_raises_threshold(self, session):
        for i in range(30):
            seed_pair(session, f"S-M{i}", 0.3, 0.1)
        summary = SERVICE.compare(session, min_count=50)
        assert summary.warning is not None


class TestCliAndApi:
    async def test_cli_report_prints_headline_and_warning(self, session, capsys):
        seed_pair(session, "R-1", 0.3, 0.1)
        count = await cli.champion_challenger_report(session=session)
        assert count == 1
        output = capsys.readouterr().out
        assert "champion/challenger: template_baseline vs baseball_evidence_v1" in output
        assert "deltas (challenger-baseline; <0 favors challenger)" in output
        assert "PAIRED (same market+outcome): pairs=1" in output
        assert "!! WARNING" in output and "do NOT infer edge" in output
        assert "by market_type (unpaired):" in output
        assert "by confidence bucket (unpaired):" in output

    async def test_cli_reports_missing_pairs_clearly(self, session, capsys):
        seed_scored(session, "R-2", forecaster="template_baseline",
                    outcome=seed_outcome(session, "R-2"))
        await cli.champion_challenger_report(session=session)
        output = capsys.readouterr().out
        assert "no same-market pairs yet" in output

    def test_main_wires_cc_report(self, monkeypatch):
        captured = {}

        async def fake_report(baseline, challenger, domain, paired_only, min_count, session=None):
            captured.update(
                baseline=baseline, challenger=challenger, domain=domain,
                paired_only=paired_only, min_count=min_count,
            )
            return 0

        monkeypatch.setattr(cli, "champion_challenger_report", fake_report)
        assert cli.main([
            "champion-challenger-report", "--baseline", "template_baseline",
            "--challenger", "baseball_evidence_v1", "--domain", "sports_baseball",
            "--paired-only", "--min-count", "30",
        ]) == 0
        assert captured == {
            "baseline": "template_baseline", "challenger": "baseball_evidence_v1",
            "domain": "sports_baseball", "paired_only": True, "min_count": 30,
        }