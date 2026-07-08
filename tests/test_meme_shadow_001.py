"""MEME-SHADOW-001 tests: read-only follow-through / calibration analysis of
MEME-MAS review_priority labels. Anchor reconstruction, horizon matching +
tolerance, price/liquidity change math, survival + rug detection, cohort
aggregation + too_thin/measured labels, calibration recommendation, no external
calls, and no forbidden trade vocabulary. Pure computation; in-memory SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot
from app.services.meme_shadow import (
    HORIZONS,
    MIN_COHORT_SAMPLES,
    MemeShadowReportService,
    MemeShadowService,
    ShadowOutcome,
    _cohort,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def add_snap(session, token, *, at, price, liq, vol=200_000.0, att=0.7, symbol="TKN",
             risk_level="low"):
    session.add(MemeAttentionSnapshot(
        token_address=token, symbol=symbol, chain="solana", price_usd=price,
        liquidity_usd=liq, volume_24h_usd=vol, attention_score=att, token_age_seconds=3600,
        boost_amount=40, boost_velocity=40, has_social=True, profile_completeness=0.9,
        liquidity_growth=0.2, volume_growth=0.2, provider_confidence=1.0,
        risk_level=risk_level, observed_at=at, created_at=at,
    ))


def add_assessment(session, token, *, flags=None, at=None, level="low"):
    session.add(CryptoTokenRiskAssessment(
        token_address=token, chain="solana", provider="risk-engine",
        flags=dict(flags or {"top10_holder_pct": 12, "sniper_pct": 1}),
        provider_names=["goplus", "solana-tracker"], risk_reasons=[],
        composite_risk_level=level, created_at=at or (NOW - timedelta(hours=6)),
    ))


# --- pipeline: anchors + horizon measurement --------------------------------


class TestPipeline:
    def test_reconstructs_anchors_and_measures_price(self, session):
        base = NOW - timedelta(hours=3)
        add_snap(session, "A", at=base, price=0.001, liq=80_000)
        add_snap(session, "A", at=base + timedelta(minutes=60), price=0.0012, liq=82_000)
        add_snap(session, "A", at=base + timedelta(minutes=360), price=0.0011, liq=79_000)
        add_assessment(session, "A", at=base - timedelta(minutes=1))
        session.commit()

        outs = MemeShadowService().outcomes(session, lookback_hours=24)
        assert len(outs) == 2  # first two snaps qualify as anchors (have later data)
        first = outs[0]
        assert first.price_change["1h"] == pytest.approx(20.0)   # 0.001 -> 0.0012
        assert first.price_change["6h"] == pytest.approx(10.0)   # 0.001 -> 0.0011
        assert first.survived is True

    def test_horizon_tolerance_excludes_out_of_band(self, session):
        base = NOW - timedelta(hours=5)
        add_snap(session, "A", at=base, price=0.001, liq=50_000)
        # later snap at 3h — inside 6h band (tol 50% => 3h..9h), NOT near 1h
        add_snap(session, "A", at=base + timedelta(minutes=180), price=0.002, liq=50_000)
        add_assessment(session, "A", at=base)
        session.commit()
        out = MemeShadowService().outcomes(session, lookback_hours=24)[0]
        assert "6h" in out.price_change            # 180m within 6h tolerance
        assert "1h" not in out.price_change        # nothing near 60m

    def test_survival_false_and_rug_on_liquidity_collapse(self, session):
        base = NOW - timedelta(hours=3)
        add_snap(session, "R", at=base, price=0.001, liq=100_000)
        add_snap(session, "R", at=base + timedelta(minutes=60), price=0.0001, liq=5_000)  # -95% liq
        add_assessment(session, "R", at=base)
        session.commit()
        out = MemeShadowService().outcomes(session, lookback_hours=24)[0]
        assert out.survived is False
        assert out.rug_or_liq_removed is True

    def test_severe_later_marks_rug(self, session):
        base = NOW - timedelta(hours=3)
        add_snap(session, "S", at=base, price=0.001, liq=80_000)
        add_snap(session, "S", at=base + timedelta(minutes=60), price=0.001, liq=80_000, risk_level="severe")
        add_assessment(session, "S", at=base)
        session.commit()
        out = MemeShadowService().outcomes(session, lookback_hours=24)[0]
        assert out.rug_or_liq_removed is True

    def test_no_anchor_without_later_data(self, session):
        # single recent snapshot -> no follow-through -> no anchors
        add_snap(session, "A", at=NOW - timedelta(minutes=2), price=0.001, liq=80_000)
        add_assessment(session, "A")
        session.commit()
        assert MemeShadowService().outcomes(session, lookback_hours=24) == []


# --- aggregation + calibration ----------------------------------------------


def outcome(priority, *, survived, rug=False, price_1h=5.0, review=0.6):
    return ShadowOutcome(
        token_address="T", review_priority=priority, review_score=review,
        structure=0.7, velocity=0.7, timing=0.7, risk_penalty=0.0, risk_reasons=[],
        top10_pct=12.0, sniper_pct=1.0, insider_pct=1.0, bundler_pct=4.0,
        risk_level_start="low", risk_level_end="low", survived=survived,
        rug_or_liq_removed=rug, price_change={"1h": price_1h}, liquidity_change={},
        volume_change={}, attention_persist={"1h": True},
    )


class FakeService:
    def __init__(self, outs):
        self._outs = outs

    def outcomes(self, session, lookback_hours=48):
        return list(self._outs)


class TestAggregation:
    def test_cohort_stats_and_too_thin_label(self):
        group = [outcome("monitor", survived=True, price_1h=3.0) for _ in range(5)]
        c = _cohort("monitor", group)
        assert c.samples == 5
        assert c.label == "too_thin"           # below MIN_COHORT_SAMPLES
        assert c.survival_rate == 1.0
        assert c.price_change_mean["1h"] == pytest.approx(3.0)

    def test_cohort_measured_label_when_enough(self):
        group = [outcome("high_review", survived=(i % 2 == 0)) for i in range(MIN_COHORT_SAMPLES)]
        c = _cohort("high_review", group)
        assert c.label == "measured"
        assert 0.0 < c.survival_rate < 1.0

    def test_calibration_labels_separate_outcomes(self):
        outs = (
            [outcome("high_review", survived=True) for _ in range(MIN_COHORT_SAMPLES)]
            + [outcome("monitor", survived=False) for _ in range(MIN_COHORT_SAMPLES)]
        )
        r = MemeShadowReportService(service=FakeService(outs)).build(session=None)
        assert r.calibration_recommendation == "labels_separate_outcomes"

    def test_calibration_inverted(self):
        outs = (
            [outcome("high_review", survived=False) for _ in range(MIN_COHORT_SAMPLES)]
            + [outcome("monitor", survived=True) for _ in range(MIN_COHORT_SAMPLES)]
        )
        r = MemeShadowReportService(service=FakeService(outs)).build(session=None)
        assert r.calibration_recommendation == "review_priority_inverted_recheck"

    def test_calibration_too_thin(self):
        outs = [outcome("high_review", survived=True) for _ in range(3)]
        r = MemeShadowReportService(service=FakeService(outs)).build(session=None)
        assert r.calibration_recommendation == "too_thin_to_calibrate"

    def test_report_has_all_breakdowns(self):
        outs = [outcome("high_review", survived=True), outcome("monitor", survived=False)]
        r = MemeShadowReportService(service=FakeService(outs)).build(session=None)
        assert r.anchors == 2
        assert r.by_review_priority and r.by_review_score_bucket
        assert r.by_risk_penalty_bucket and r.by_concentration
        assert set(r.horizon_coverage) == {h for h, _ in HORIZONS}


# --- safety -----------------------------------------------------------------


def test_no_forbidden_trade_vocabulary_in_report():
    outs = [outcome("high_review", survived=True), outcome("reject_risk", survived=False, rug=True)]
    r = MemeShadowReportService(service=FakeService(outs)).build(session=None)
    blob = " ".join(
        [r.calibration_recommendation]
        + [c["cohort"] + " " + c["label"] for c in r.by_review_priority + r.by_concentration]
    ).lower()
    for term in ("buy", "sell", " bet", "trade", "profit", "kelly", "position_siz",
                 "wallet", "swap", "recommend_trade"):
        assert term not in blob, f"forbidden term {term!r} in shadow report"


def test_service_module_makes_no_network_calls():
    import app.services.meme_shadow as m
    with open(m.__file__) as fh:
        text = fh.read()
    assert "httpx" not in text and "requests" not in text and "AsyncClient" not in text
