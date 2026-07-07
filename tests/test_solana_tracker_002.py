"""SOLANA-TRACKER-002: sniper/insider/bundler coverage from the SolanaTracker
`/tokens/{address}` risk object. The data was always fetched (same endpoint, no
extra request) but mis-parsed — the concentration fields arrive as
`{"count", "totalBalance", "totalPercentage", "wallets"}` and the field is
`totalPercentage` (a 0-100 percent), not the old `percentage`. These tests pin
the real payload shape (mocked — no live network) and the graceful fallbacks.

Read-only risk intelligence only — no EV/trade/sizing/orders/wallets/execution.
"""

import pytest

from app.services.crypto_risk import SolanaTrackerRiskAdapter
from tests.test_crypto_risk_engine import evaluate
from app.services.crypto_risk_engine import CAT_BUNDLER, CAT_INSIDER, CAT_SNIPER

ADAPTER = SolanaTrackerRiskAdapter()


def risk(**risk_fields):
    return {"token": {"name": "X"}, "risk": {"score": 3, "risks": [], **risk_fields}}


# --- payload shape ----------------------------------------------------------


def test_total_percentage_shape_populates_all_three():
    """The confirmed live shape: concentration fields as dicts with
    totalPercentage. bundlers carries real signal; snipers/insiders present."""
    payload = risk(
        snipers={"count": 0, "totalBalance": 0, "totalPercentage": 0, "wallets": []},
        insiders={"count": 2, "totalBalance": 10, "totalPercentage": 4.5, "wallets": []},
        bundlers={"count": 297, "totalBalance": 21254203.0, "totalPercentage": 2.1256, "wallets": []},
        top10=25.1686,
    )
    flags = ADAPTER.parse("TK", payload).flags
    assert flags["sniper_pct"] == 0.0        # present (0%), so coverage counts
    assert flags["insider_pct"] == 4.5
    assert flags["bundler_pct"] == 2.1256
    assert flags["top10_holder_pct"] == 25.1686


def test_zero_percent_is_present_not_absent():
    flags = ADAPTER.parse("TK", risk(snipers={"totalPercentage": 0})).flags
    assert "sniper_pct" in flags and flags["sniper_pct"] == 0.0


def test_low_percentage_is_not_mis_scaled():
    """A totalPercentage below 1 must stay a percentage (0.7 -> 0.7%), never be
    treated as a ratio and multiplied to 70%."""
    flags = ADAPTER.parse("TK", risk(
        snipers={"totalPercentage": 0.7},
        bundlers={"totalPercentage": 0.25},
    )).flags
    assert flags["sniper_pct"] == 0.7
    assert flags["bundler_pct"] == 0.25


def test_missing_keys_stay_absent_no_fabrication():
    flags = ADAPTER.parse("TK", risk(top10=20.0)).flags
    assert "sniper_pct" not in flags
    assert "insider_pct" not in flags
    assert "bundler_pct" not in flags
    assert flags["top10_holder_pct"] == 20.0


def test_non_numeric_total_percentage_is_graceful():
    flags = ADAPTER.parse("TK", risk(bundlers={"totalPercentage": None},
                                      insiders={"totalPercentage": "n/a"})).flags
    assert "bundler_pct" not in flags
    assert "insider_pct" not in flags


def test_legacy_percentage_ratio_fallback_preserved():
    """If a payload variant returns the old `percentage` (0-1 ratio) with no
    totalPercentage, it still parses (backward compatible)."""
    flags = ADAPTER.parse("TK", risk(snipers={"percentage": 0.31})).flags
    assert flags["sniper_pct"] == 31.0


def test_clamped_to_100():
    flags = ADAPTER.parse("TK", risk(bundlers={"totalPercentage": 250})).flags
    assert flags["bundler_pct"] == 100.0


# --- heuristic activation (dormant categories now fire on real data) --------


def test_high_bundler_concentration_fires_category():
    _, reasons = evaluate(provider_flags={"bundler_pct": 30.0}, provider_backed=True)  # > 25 threshold
    assert CAT_BUNDLER in reasons


def test_high_sniper_and_insider_fire_categories():
    _, sniper_reasons = evaluate(provider_flags={"sniper_pct": 25.0}, provider_backed=True)  # > 20
    assert CAT_SNIPER in sniper_reasons
    _, insider_reasons = evaluate(provider_flags={"insider_pct": 20.0}, provider_backed=True)  # > 15
    assert CAT_INSIDER in insider_reasons


def test_low_concentration_does_not_fire_categories():
    _, reasons = evaluate(
        provider_flags={"sniper_pct": 0.0, "insider_pct": 4.5, "bundler_pct": 2.1256},
        provider_backed=True,
    )
    assert CAT_SNIPER not in reasons
    assert CAT_INSIDER not in reasons
    assert CAT_BUNDLER not in reasons
