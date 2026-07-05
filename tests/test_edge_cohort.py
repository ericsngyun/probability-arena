"""Edge cohort analysis (EDGE-ANALYSIS-001) tests: cohort grouping across
every dimension, per-cohort gap follow-through, the conservative label ladder
(too_thin / promising / neutral / weak / exclude_candidate), the MVP-005B
gate, and CLI wiring. Analysis only — no live network, no trade semantics."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.models import (
    EdgePrecheckSnapshot,
    MarketForecastRecord,
    MarketPriceTick,
    OpportunitySignal,
)
from app.services.edge_cohort import (
    LABEL_EXCLUDE,
    LABEL_NEUTRAL,
    LABEL_PROMISING,
    LABEL_TOO_THIN,
    LABEL_WEAK,
    MIN_COHORT_FOLLOW_SAMPLES,
    EdgeCohortReportService,
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
    conf=0.65,
    spread=1,
    liquidity=2_000_000,
    market_type="total",
    domain="sports_baseball",
    signal_type=None,
    phase_tags=None,
):
    """Seed one edge snapshot with explicit cohort attributes. If `toward` is
    set, also seed a single later tick 5 minutes after the snapshot that moves
    the midpoint toward (True) or away (False) from the forecast, so the
    snapshot yields one follow-through sample per horizon."""
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
    forecast_id = f.id
    signal_id = None
    if signal_type is not None:
        s = OpportunitySignal(
            market_ticker=ticker,
            signal_type=signal_type,
            signal_status="forecast_refreshed",
            observed_at=NOW - timedelta(minutes=minutes_ago + 2),
            reason="seeded",
            created_at=NOW - timedelta(minutes=minutes_ago + 2),
        )
        session.add(s)
        session.commit()
        signal_id = s.id

    row = EdgePrecheckSnapshot(
        market_ticker=ticker,
        signal_id=signal_id,
        forecast_id=forecast_id,
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
    return row


def seed_cohort(session, n, *, prefix, toward_count, **kwargs):
    """Seed `n` snapshots on unique tickers; `toward_count` of them move
    toward the forecast, the rest away. Each gets its own follow-through tick."""
    for i in range(n):
        seed_snap(
            session,
            f"{prefix}-{i}",
            toward=(i < toward_count),
            **kwargs,
        )


def service() -> EdgeCohortReportService:
    return EdgeCohortReportService()


def cohort(rendered_dim: list[dict], key: str) -> dict:
    for c in rendered_dim:
        if c["key"] == key:
            return c
    raise AssertionError(f"cohort {key!r} not found in {[c['key'] for c in rendered_dim]}")


class TestCohortGrouping:
    def test_groups_every_dimension(self, session):
        seed_snap(
            session, "KXMLBTOTAL-A", market_type="total", domain="sports_baseball",
            gap=0.12, conf=0.65, spread=1, liquidity=2_000_000, persistence=1,
            signal_type="price_move_threshold", phase_tags=["late_game"],
        )
        seed_snap(
            session, "KXWCSPREAD-B", market_type="spread", domain="sports_soccer",
            gap=-0.08, conf=0.60, spread=4, liquidity=50_000, persistence=3,
            status="paper_candidate_later",
            signal_type="spread_tightened", phase_tags=["early_game"],
        )
        report = service().build(session, hours=24)
        dims = report.dimensions

        assert report.total_snapshots == 2
        assert {c["key"] for c in dims["market_type"]} == {"total", "spread"}
        assert {c["key"] for c in dims["domain"]} == {"sports_baseball", "sports_soccer"}
        assert {c["key"] for c in dims["gap_sign"]} == {"positive", "negative"}
        assert {c["key"] for c in dims["abs_gap_bucket"]} == {"0.10-0.15", "0.075-0.10"}
        assert {c["key"] for c in dims["confidence_bucket"]} == {"0.65+", "0.60"}
        assert {c["key"] for c in dims["signal_type"]} == {
            "price_move_threshold", "spread_tightened"
        }
        assert {c["key"] for c in dims["liquidity_bucket"]} == {"1M-10M", "<100k"}
        assert {c["key"] for c in dims["spread_bucket"]} == {"1", "3-5"}
        assert {c["key"] for c in dims["game_phase"]} == {"late", "early"}
        assert {c["key"] for c in dims["persistence"]} == {"1", "3+"}

    def test_counts_watchlist_candidate_and_invalid(self, session):
        seed_snap(session, "KXMLBTOTAL-A", status="watchlist", market_type="total")
        seed_snap(session, "KXMLBTOTAL-B", status="paper_candidate_later",
                  market_type="total", persistence=3)
        seed_snap(session, "KXMLBTOTAL-C", status="invalid_wide_spread",
                  market_type="total", gap=None, spread=40)
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["sample"] == 3
        assert total["watchlist"] == 1
        assert total["paper_candidate_later"] == 1
        assert total["invalid"] == 1
        assert total["invalid_rate"] == pytest.approx(1 / 3, abs=1e-3)


class TestFollowThroughByCohort:
    def test_toward_movement_scored_per_cohort(self, session):
        # 3 snapshots all moving toward the forecast; one tick each.
        seed_cohort(session, 3, prefix="KXMLBTOTAL", toward_count=3, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        ft = total["follow_through"]
        # one tick 5 min after each snapshot => a sample at every horizon
        for h in ("5m", "15m", "30m", "60m"):
            assert ft[h]["samples"] == 3
            assert ft[h]["moved_toward_rate"] == 1.0
        assert total["blended_toward_rate"] == 1.0

    def test_away_movement_counts_against(self, session):
        seed_cohort(session, 3, prefix="KXMLBTOTAL", toward_count=0, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["follow_through"]["15m"]["moved_toward_rate"] == 0.0
        assert total["blended_toward_rate"] == 0.0

    def test_invalid_rows_have_no_follow_through(self, session):
        seed_snap(session, "KXMLBTOTAL-A", status="invalid_wide_spread",
                  gap=None, toward=None, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["follow_through_samples"] == 0
        assert total["blended_toward_rate"] is None


class TestLabels:
    def test_thin_sample_labeled_too_thin(self, session):
        # High toward rate but only a few samples -> too_thin, never promising.
        n = MIN_COHORT_FOLLOW_SAMPLES - 2
        seed_cohort(session, n, prefix="KXMLBTOTAL", toward_count=n, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["follow_through_samples"] < MIN_COHORT_FOLLOW_SAMPLES
        assert total["recommendation"] == LABEL_TOO_THIN

    def test_promising_requires_minimum_sample(self, session):
        # Same perfect toward rate, but now over the sample floor -> promising.
        n = MIN_COHORT_FOLLOW_SAMPLES + 2
        seed_cohort(session, n, prefix="KXMLBTOTAL", toward_count=n, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["follow_through_samples"] >= MIN_COHORT_FOLLOW_SAMPLES
        assert total["recommendation"] == LABEL_PROMISING

    def test_weak_cohort_labeled_weak(self, session):
        # 5/12 toward => blended ~0.417 -> weak (between exclude and neutral).
        n = 12
        seed_cohort(session, n, prefix="KXMLBTOTAL", toward_count=5, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["blended_toward_rate"] == pytest.approx(5 / 12, abs=1e-3)
        assert total["recommendation"] == LABEL_WEAK

    def test_all_away_labeled_exclude_candidate(self, session):
        n = 14
        seed_cohort(session, n, prefix="KXMLBTOTAL", toward_count=0, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["recommendation"] == LABEL_EXCLUDE

    def test_even_split_labeled_neutral(self, session):
        n = 12
        seed_cohort(session, n, prefix="KXMLBTOTAL", toward_count=6, market_type="total")
        total = cohort(service().build(session).dimensions["market_type"], "total")
        assert total["blended_toward_rate"] == pytest.approx(0.5, abs=1e-3)
        assert total["recommendation"] == LABEL_NEUTRAL


class TestMvp005bGate:
    def test_blocked_when_follow_through_weak(self, session):
        seed_cohort(session, 14, prefix="KXMLBTOTAL", toward_count=0, market_type="total")
        report = service().build(session)
        assert report.mvp_005b_blocked is True
        assert "BLOCKED" in report.mvp_005b_reason
        # weak/exclude cohorts surface as deprioritize, not promising
        assert report.promising == []
        assert any("market_type=total" in d for d in report.deprioritize)

    def test_blocked_when_promising_but_below_design_sample_floor(self, session):
        # promising at the cohort floor (12) but below the stricter 005B floor (20)
        seed_cohort(session, 14, prefix="KXMLBTOTAL", toward_count=14, market_type="total")
        report = service().build(session)
        assert any("market_type=total" in p for p in report.promising)
        assert report.mvp_005b_blocked is True

    def test_gate_reports_support_only_with_strong_and_overall(self, session):
        # 24 all-toward snapshots: clears both the strict cohort floor and overall.
        seed_cohort(session, 24, prefix="KXMLBTOTAL", toward_count=24, market_type="total")
        report = service().build(session)
        assert report.mvp_005b_blocked is False
        # Even when data supports it, the gate never unlocks capability itself.
        assert "human acceptance" in report.mvp_005b_reason


class TestWindowAndSafety:
    def test_window_excludes_old_rows(self, session):
        seed_snap(session, "KXMLBTOTAL-OLD", minutes_ago=60 * 40, market_type="total")
        seed_snap(session, "KXMLBTOTAL-NEW", minutes_ago=30, market_type="total")
        report = service().build(session, hours=24)
        assert report.total_snapshots == 1

    def test_report_note_states_analysis_only(self, session):
        seed_snap(session, "KXMLBTOTAL-A", market_type="total")
        report = service().build(session)
        assert "not advice" in report.note
        assert "authorize no trade" in report.note


def test_main_wires_edge_cohort_report(monkeypatch):
    captured = {}

    async def fake_report(hours=24, session=None):
        captured["hours"] = hours
        return 0

    monkeypatch.setattr(cli, "edge_cohort_report", fake_report)
    exit_code = cli.main(["edge-cohort-report", "--hours", "12"])
    assert exit_code == 0
    assert captured["hours"] == 12


def test_cli_prints_report(session, capsys):
    import asyncio

    seed_cohort(session, 14, prefix="KXMLBTOTAL", toward_count=0, market_type="total")
    asyncio.run(cli.edge_cohort_report(hours=24, session=session))
    out = capsys.readouterr().out
    assert "edge cohort analysis" in out
    assert "cohort: market_type" in out
    assert "MVP-005B-design gate" in out
    assert "blocked: True" in out
