"""MEME-MAS-001 tests: read-only multi-agent diagnostic scoring. Sub-score math,
severe-risk -> reject_risk, missing-coverage penalty, high attention + good
structure -> elevated/high review, concentration penalties, the windowed report,
and — critically — NO forbidden trade vocabulary in serialized diagnostic output.
Pure computation; no live network; in-memory SQLite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import CryptoTokenRiskAssessment, MemeAttentionSnapshot, MemeCatalystEvent
from app.services.meme_mas import (
    REVIEW_PRIORITIES,
    MemeMasDiagnosticService,
    MemeMasReportService,
    TokenInputs,
    catalyst_velocity_agent,
    coin_structure_agent,
    composite_review_agent,
    risk_auditor_agent,
    timing_agent,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def snap(token="A", *, symbol="TKN", liq=80_000.0, vol=200_000.0, attention=0.7,
         age=3600, boost=50.0, social=True, completeness=0.9, liq_growth=0.2,
         vol_growth=0.3, conf=1.0, risk_level="low", observed_at=None):
    return MemeAttentionSnapshot(
        token_address=token, symbol=symbol, chain="solana", liquidity_usd=liq,
        volume_24h_usd=vol, attention_score=attention, token_age_seconds=age,
        boost_amount=boost, boost_velocity=(boost or 0), has_social=social,
        profile_completeness=completeness, liquidity_growth=liq_growth,
        volume_growth=vol_growth, provider_confidence=conf, risk_level=risk_level,
        observed_at=observed_at or NOW, created_at=observed_at or NOW,
    )


def assess_row(token="A", *, flags=None, providers=("goplus", "solana-tracker"),
               reasons=(), level="low"):
    return CryptoTokenRiskAssessment(
        token_address=token, chain="solana", provider="risk-engine",
        flags=dict(flags or {}), provider_names=list(providers),
        risk_reasons=list(reasons), composite_risk_level=level, created_at=NOW,
    )


def inputs(token="A", *, s=None, prev=None, a=None, catalysts=4, snaps=3):
    return TokenInputs(
        token_address=token, symbol=(s.symbol if s else "TKN"),
        snapshot=s or snap(token), previous=prev, assessment=a,
        catalyst_count=catalysts, snapshot_count=snaps, source_snapshot_ids=[1],
    )


# --- sub-score math ---------------------------------------------------------


class TestSubScores:
    def test_structure_high_for_clean_token(self):
        r = coin_structure_agent(inputs(a=assess_row(flags={
            "top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 3})))
        assert r.score >= 0.8
        assert "healthy_liquidity" in r.reasons

    def test_structure_penalized_by_concentration(self):
        clean = coin_structure_agent(inputs(a=assess_row(flags={
            "top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 3})))
        dirty = coin_structure_agent(inputs(a=assess_row(flags={
            "top10_holder_pct": 70, "sniper_pct": 40, "insider_pct": 30, "bundler_pct": 60})))
        assert dirty.score < clean.score
        assert "high_top10_concentration" in dirty.reasons
        assert "sniper_concentration_flagged" in dirty.reasons
        assert "bundler_concentration_flagged" in dirty.reasons

    def test_velocity_rewards_attention_and_rise(self):
        prev = snap(attention=0.4)
        cur = snap(attention=0.7)
        strong = catalyst_velocity_agent(inputs(s=cur, prev=prev))
        weak = catalyst_velocity_agent(inputs(s=snap(attention=0.15), catalysts=0))
        assert strong.score > weak.score
        assert "strong_attention" in strong.reasons
        assert "attention_rising" in strong.reasons

    def test_timing_rewards_fresh_and_momentum(self):
        fresh = timing_agent(inputs(s=snap(age=1800, liq_growth=0.3, vol_growth=0.3)))
        stale = timing_agent(inputs(s=snap(age=10 * 24 * 3600, liq_growth=-0.3, vol_growth=-0.2), snaps=1))
        assert fresh.score > stale.score
        assert "fresh_token" in fresh.reasons
        assert "mature_token" in stale.reasons

    def test_missing_coverage_penalizes_and_is_flagged(self):
        r = risk_auditor_agent(inputs(a=None))  # no assessment -> no provider data
        assert "missing_provider_coverage" in r.reasons
        assert "provider_risk_data" in r.missing
        assert r.score > 0.0


# --- composite review -------------------------------------------------------


class TestComposite:
    def test_severe_risk_forces_reject(self):
        r = MemeMasDiagnosticService().assess(inputs(
            s=snap(attention=0.9, risk_level="severe"),
            a=assess_row(flags={"rug_risk": True}, reasons=["fake_volume_suspected"], level="severe"),
        ))
        assert r.review_priority == "reject_risk"
        assert "severe_risk_level" in r.risk_reasons
        assert "rug_flag" in r.risk_reasons

    def test_rug_or_honeypot_forces_reject_even_if_attractive(self):
        r = MemeMasDiagnosticService().assess(inputs(
            s=snap(attention=0.8), a=assess_row(flags={"honeypot": True}, level="medium")))
        assert r.review_priority == "reject_risk"

    def test_high_attention_good_structure_elevated_or_high(self):
        r = MemeMasDiagnosticService().assess(inputs(
            s=snap(attention=0.75), a=assess_row(flags={
                "top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 3})))
        assert r.review_priority in ("elevated_review", "high_review")
        assert r.risk_penalty < 0.5

    def test_weak_signal_maps_low_or_monitor(self):
        r = MemeMasDiagnosticService().assess(inputs(
            s=snap(attention=0.1, liq=800, vol=400, age=10 * 24 * 3600, boost=0,
                   social=False, completeness=0.1, liq_growth=-0.2, vol_growth=-0.2),
            a=assess_row(flags={"top10_holder_pct": 12}), catalysts=0, snaps=1))
        assert r.review_priority in ("low", "monitor")

    def test_priority_is_always_valid_label(self):
        r = MemeMasDiagnosticService().assess(inputs())
        assert r.review_priority in REVIEW_PRIORITIES


# --- report -----------------------------------------------------------------


class TestReport:
    def _seed(self, session):
        # clean/attractive token
        session.add(snap("GOOD", symbol="GOOD"))
        session.add(assess_row("GOOD", flags={
            "top10_holder_pct": 12, "sniper_pct": 1, "insider_pct": 1, "bundler_pct": 3}))
        # severe rug token
        session.add(snap("RUG", symbol="RUG", liq=800, vol=500, attention=0.6, risk_level="severe"))
        session.add(assess_row("RUG", flags={"rug_risk": True, "top10_holder_pct": 85},
                               reasons=["fake_volume_suspected"], level="severe"))
        # token with no provider assessment (missing coverage)
        session.add(snap("BARE", symbol="BARE", attention=0.5))
        for _ in range(3):
            session.add(MemeCatalystEvent(subject_ref="GOOD", source="dexscreener",
                                          subject_type="token", catalyst_type="boost",
                                          observed_at=NOW, created_at=NOW))
        session.commit()

    def test_report_builds_and_triages(self, session):
        self._seed(session)
        r = MemeMasReportService().build(session, hours=24)
        assert r.tokens_assessed == 3
        assert sum(r.by_priority.values()) == 3
        assert r.by_priority["reject_risk"] >= 1                 # RUG
        assert any(c["symbol"] == "GOOD" for c in r.top_candidates)
        assert all(c["review_priority"] != "reject_risk" for c in r.top_candidates)
        assert r.missing_coverage_tokens >= 1                    # BARE
        assert r.subscore_distributions["structure_p50"] is not None

    def test_no_forbidden_trade_vocabulary_in_diagnostic_output(self, session):
        """The serialized DIAGNOSTIC data (priorities, reasons, traces, missing)
        must never contain trade/EV vocabulary. (The disclaimer note, which
        states what it is NOT, is excluded — that is a boundary statement.)"""
        self._seed(session)
        results = MemeMasReportService().assess_all(session, hours=24)
        blob = " ".join(
            [r.review_priority for r in results]
            + [w for r in results for w in r.reasoning_trace]
            + [w for r in results for w in r.risk_reasons]
            + [w for r in results for w in r.missing_evidence]
        ).lower()
        for term in ("buy", "sell", " bet", "trade", "profit", "kelly",
                     "position_siz", "wallet", "swap", "order", "recommend", " ev "):
            assert term not in blob, f"forbidden term {term!r} in diagnostic output"


# --- no external calls ------------------------------------------------------


def test_service_module_imports_no_network_client():
    import app.services.meme_mas as m
    src = (m.__file__)
    with open(src) as fh:
        text = fh.read()
    assert "httpx" not in text
    assert "requests" not in text
    assert "AsyncClient" not in text
