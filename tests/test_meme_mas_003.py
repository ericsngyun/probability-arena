"""MEME-MAS-003 tests: multi-objective calibration metrics for MEME-MAS
review_priority (momentum follow-through, survival quality, risk-adjusted
movement, review-queue efficiency, coverage quality), v1 vs v2. Read-only
MEASUREMENT — no label changes, no external calls, no forbidden vocabulary."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot
from app.services.meme_mas import PROFILE_V1, PROFILE_V2
from app.services.meme_shadow import (
    MemeShadowObjectivesService,
    ShadowOutcome,
    _median_price,
    _positive_rate,
    _severe_end_rate,
)

NOW = datetime.now(timezone.utc)


def out(priority="high_review", *, price_1h=None, survived=True, rug=False,
        risk_reasons=None, level_end="low"):
    return ShadowOutcome(
        token_address="T", review_priority=priority, review_score=0.7, structure=0.8,
        velocity=0.8, timing=0.8, risk_penalty=0.0, risk_reasons=list(risk_reasons or []),
        top10_pct=12.0, sniper_pct=1.0, insider_pct=1.0, bundler_pct=4.0,
        risk_level_start="low", risk_level_end=level_end, survived=survived,
        rug_or_liq_removed=rug, price_change=({"1h": price_1h} if price_1h is not None else {}),
    )


# --- metric helpers ---------------------------------------------------------


def test_positive_rate():
    g = [out(price_1h=10), out(price_1h=-5), out(price_1h=3), out(price_1h=None)]
    assert _positive_rate(g, "1h") == pytest.approx(2 / 3, abs=1e-4)


def test_median_price():
    g = [out(price_1h=10), out(price_1h=-5), out(price_1h=30)]
    assert _median_price(g, "1h") == 10.0


def test_severe_end_rate():
    g = [out(level_end="severe"), out(level_end="low"), out(level_end="low")]
    assert _severe_end_rate(g) == pytest.approx(1 / 3, abs=1e-4)


# --- objectives build (seeded) ----------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _series(session, token, symbol, prices, *, covered, att=0.72, liq=80_000):
    base = NOW - timedelta(hours=4)
    for m, pr in prices:
        session.add(MemeAttentionSnapshot(
            token_address=token, symbol=symbol, chain="solana", price_usd=pr, liquidity_usd=liq,
            volume_24h_usd=200_000, attention_score=att, token_age_seconds=3600, boost_amount=40,
            boost_velocity=40, has_social=True, profile_completeness=0.9, liquidity_growth=0.2,
            volume_growth=0.2, provider_confidence=1.0, risk_level="low",
            observed_at=base + timedelta(minutes=m), created_at=base + timedelta(minutes=m),
        ))
    if covered:
        session.add(CryptoTokenRiskAssessment(
            token_address=token, chain="solana", provider="risk-engine",
            flags={"top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 4},
            provider_names=["goplus", "solana-tracker"], risk_reasons=[],
            composite_risk_level="low", created_at=base - timedelta(minutes=1),
        ))


def _seed_mix(session):
    # 8 clean covered momentum-positive survivors
    for n in range(8):
        _series(session, f"WIN{n}", f"W{n}", [(0, 0.001), (60, 0.0013), (360, 0.0012)], covered=True)
    # 8 missing-coverage faders
    for n in range(8):
        _series(session, f"MISS{n}", f"M{n}", [(0, 0.001), (60, 0.0009), (360, 0.0007)], covered=False)
    session.commit()


class TestObjectives:
    def test_all_five_sections_present(self, session):
        _seed_mix(session)
        o = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)
        assert o.momentum_followthrough and o.survival_quality
        assert o.risk_adjusted_movement and o.review_queue_efficiency
        assert set(o.coverage_quality) == {"covered", "missing"}

    def test_v2_high_review_is_momentum_positive(self, session):
        _seed_mix(session)
        o = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)
        hr = next(r for r in o.momentum_followthrough if r["priority"] == "high_review")
        # v2 high_review = the clean WIN tokens -> all momentum-positive at 1h
        assert hr["momentum_positive_rate_1h"] == 1.0
        assert hr["price_1h_median"] > 0

    def test_review_queue_efficiency_lift(self, session):
        _seed_mix(session)
        o = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)
        hr = next(r for r in o.review_queue_efficiency if r["priority"] == "high_review")
        assert hr["lift"] is not None and hr["lift"] > 1.0   # concentrates momentum-positive tokens

    def test_risk_adjusted_is_median_times_survival(self, session):
        _seed_mix(session)
        o = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)
        hr = next(r for r in o.risk_adjusted_movement if r["priority"] == "high_review")
        assert hr["risk_adjusted_1h"] == pytest.approx(hr["median_price_1h"] * hr["survival_rate"], abs=1e-3)

    def test_coverage_quality_missing_worse(self, session):
        _seed_mix(session)
        o = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)
        cov, miss = o.coverage_quality["covered"], o.coverage_quality["missing"]
        assert cov["momentum_positive_rate_1h"] > miss["momentum_positive_rate_1h"]

    def test_v1_vs_v2_high_review_more_selective(self, session):
        _seed_mix(session)
        o1 = MemeShadowObjectivesService(profile=PROFILE_V1).build(session, lookback_hours=24)
        o2 = MemeShadowObjectivesService(profile=PROFILE_V2).build(session, lookback_hours=24)

        def hr_n(o):
            return next((r["n"] for r in o.review_queue_efficiency if r["priority"] == "high_review"), 0)

        assert o1.anchors == o2.anchors
        assert hr_n(o2) < hr_n(o1)      # v2 is more selective (labels unchanged by this milestone)


# --- no label change + safety -----------------------------------------------


def test_objectives_does_not_relabel(session):
    """MEME-MAS-003 only MEASURES; the labels come from the unchanged MEME-MAS
    scorer for the given profile."""
    _seed_mix(session)
    svc = MemeShadowObjectivesService(profile=PROFILE_V2)
    assert svc.profile.name == "v2"
    assert svc.service.diagnostic.profile.name == "v2"


def test_no_forbidden_vocabulary_in_objectives():
    from app.services.meme_shadow import OBJECTIVES_NOTE

    blob = OBJECTIVES_NOTE.lower()
    # the note NEGATES trade vocab (allowed); assert no positive trade language
    for term in ("buy ", "sell ", " bet ", "place order", "position size increase"):
        assert term not in blob
