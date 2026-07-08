"""MEME-MAS-002 recalibration tests: v2 makes high_review risk-aware and
selective. Missing-provider-coverage and concentration-flagged tokens are
demoted out of high_review; risk penalties are heavier; reject_risk hard gates
are preserved; momentum/structure/coverage quality are first-class outputs; and
the v1->v2 high_review share drops. v1 still reproduces MEME-MAS-001. Pure
computation; no external calls."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot
from app.services.meme_mas import (
    DEFAULT_PROFILE,
    PROFILE_V1,
    PROFILE_V2,
    MemeMasDiagnosticService,
    TokenInputs,
    coverage_quality_agent,
    risk_auditor_agent,
)
from app.services.meme_shadow import MemeShadowReportService

NOW = datetime.now(timezone.utc)


def snap(*, att=0.75, liq=80_000.0, vol=200_000.0, conf=1.0, risk_level="low", symbol="TKN"):
    return MemeAttentionSnapshot(
        token_address="X", symbol=symbol, chain="solana", price_usd=0.001, liquidity_usd=liq,
        volume_24h_usd=vol, attention_score=att, token_age_seconds=3600, boost_amount=40,
        boost_velocity=40, has_social=True, profile_completeness=0.9, liquidity_growth=0.2,
        volume_growth=0.3, provider_confidence=conf, risk_level=risk_level,
        observed_at=NOW, created_at=NOW,
    )


def assess_row(flags=None, providers=("goplus", "solana-tracker"), reasons=(), level="low"):
    return CryptoTokenRiskAssessment(
        token_address="X", chain="solana", provider="risk-engine", flags=dict(flags or {}),
        provider_names=list(providers), risk_reasons=list(reasons),
        composite_risk_level=level, created_at=NOW,
    )


def inp(s=None, a=None):
    return TokenInputs("X", "TKN", s or snap(), None, a, catalyst_count=4, snapshot_count=3,
                       source_snapshot_ids=[1])


CLEAN_FLAGS = {"top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 4}


# --- defaults + quality outputs ---------------------------------------------


def test_default_profile_is_v2():
    assert DEFAULT_PROFILE.name == "v2"
    assert MemeMasDiagnosticService().profile.name == "v2"


def test_quality_outputs_present():
    r = MemeMasDiagnosticService().assess(inp(a=assess_row(CLEAN_FLAGS)))
    assert 0.0 <= r.momentum_quality <= 1.0
    assert r.structure_quality == r.structure_score
    assert 0.0 <= r.coverage_quality <= 1.0
    d = r.scores()
    assert {"momentum_quality", "structure_quality", "coverage_quality"} <= set(d)


def test_coverage_quality_low_when_missing_provider():
    covered = coverage_quality_agent(inp(a=assess_row(CLEAN_FLAGS)))
    missing = coverage_quality_agent(inp(a=None))
    assert covered.score > missing.score
    assert "missing_provider_coverage" in missing.reasons


# --- heavier risk penalties -------------------------------------------------


def test_v2_missing_coverage_penalty_heavier_than_v1():
    v1 = risk_auditor_agent(inp(a=None), PROFILE_V1)
    v2 = risk_auditor_agent(inp(a=None), PROFILE_V2)
    assert v2.score > v1.score          # 0.55 vs 0.3
    assert "missing_provider_coverage" in v2.reasons


def test_v2_concentration_penalty_heavier():
    row = assess_row({"top10_holder_pct": 70, "bundler_pct": 60})
    v1 = risk_auditor_agent(inp(a=row), PROFILE_V1)
    v2 = risk_auditor_agent(inp(a=row), PROFILE_V2)
    assert v2.score >= v1.score         # 0.7 vs 0.6


# --- high_review gating -----------------------------------------------------


class TestHighReviewGating:
    def test_clean_covered_token_stays_high_review(self):
        r = MemeMasDiagnosticService(PROFILE_V2).assess(inp(a=assess_row(CLEAN_FLAGS)))
        assert r.review_priority == "high_review"

    def test_missing_coverage_demoted_from_high_review(self):
        v1 = MemeMasDiagnosticService(PROFILE_V1).assess(inp(a=None))
        v2 = MemeMasDiagnosticService(PROFILE_V2).assess(inp(a=None))
        assert v1.review_priority == "high_review"       # old behavior
        assert v2.review_priority != "high_review"       # gated out
        assert "missing_provider_coverage" in v2.risk_reasons

    def test_concentration_flag_demoted(self):
        row = assess_row({"top10_holder_pct": 70, "bundler_pct": 60})
        v2 = MemeMasDiagnosticService(PROFILE_V2).assess(inp(a=row))
        assert v2.review_priority not in ("high_review",)
        assert any("concentration" in r for r in v2.risk_reasons)

    def test_reject_risk_hard_gate_preserved_both_profiles(self):
        row = assess_row({"rug_risk": True}, level="severe")
        s = snap(risk_level="severe")
        for profile in (PROFILE_V1, PROFILE_V2):
            r = MemeMasDiagnosticService(profile).assess(inp(s=s, a=row))
            assert r.review_priority == "reject_risk"


# --- v1 reproduces MEME-MAS-001 ---------------------------------------------


def test_v1_profile_has_no_gates():
    # a strong but missing-coverage token was high_review in v1 (no gate)
    r = MemeMasDiagnosticService(PROFILE_V1).assess(inp(a=None))
    assert r.review_priority == "high_review"


# --- before/after via MEME-SHADOW -------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed_series(session, token, *, symbol, covered, flags=None):
    base = NOW - timedelta(hours=4)
    for m, pr in [(0, 0.001), (60, 0.0012), (360, 0.0011)]:
        session.add(MemeAttentionSnapshot(
            token_address=token, symbol=symbol, chain="solana", price_usd=pr, liquidity_usd=80_000,
            volume_24h_usd=200_000, attention_score=0.72, token_age_seconds=3600, boost_amount=40,
            boost_velocity=40, has_social=True, profile_completeness=0.9, liquidity_growth=0.2,
            volume_growth=0.2, provider_confidence=1.0, risk_level="low",
            observed_at=base + timedelta(minutes=m), created_at=base + timedelta(minutes=m),
        ))
    if covered:
        session.add(CryptoTokenRiskAssessment(
            token_address=token, chain="solana", provider="risk-engine",
            flags=dict(flags or CLEAN_FLAGS), provider_names=["goplus", "solana-tracker"],
            risk_reasons=[], composite_risk_level="low", created_at=base - timedelta(minutes=1),
        ))


def test_high_review_share_reduced_v1_to_v2(session):
    for n in range(6):
        _seed_series(session, f"CLEAN{n}", symbol=f"C{n}", covered=True)
    for n in range(6):
        _seed_series(session, f"MISS{n}", symbol=f"M{n}", covered=False)  # missing coverage
    session.commit()

    r1 = MemeShadowReportService(profile=PROFILE_V1).build(session, lookback_hours=24)
    r2 = MemeShadowReportService(profile=PROFILE_V2).build(session, lookback_hours=24)

    def high_n(r):
        return next((c["samples"] for c in r.by_review_priority if c["cohort"] == "high_review"), 0)

    assert r1.anchors == r2.anchors
    assert high_n(r2) < high_n(r1)        # v2 is more selective
    # v2 high_review should be the covered tokens only (missing-coverage demoted)
    assert high_n(r2) <= 12               # 6 clean tokens x 2 anchors
