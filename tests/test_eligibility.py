from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.services.eligibility import (
    REASON_EXPIRES_TOO_FAR,
    REASON_EXPIRES_TOO_SOON,
    REASON_LIQUIDITY_BELOW_MIN,
    REASON_MISSING_EXPIRATION,
    REASON_NO_QUOTES,
    REASON_ONE_SIDED_QUOTE,
    REASON_SPREAD_TOO_WIDE,
    REASON_VOLUME_24H_BELOW_MIN,
    WARNING_PARLAY_LIKE,
    EligibilityThresholds,
    assess_market,
)
from tests.conftest import make_market

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
THRESHOLDS = EligibilityThresholds()  # spec defaults, independent of env


def days_out(days: float) -> datetime:
    return NOW + timedelta(days=days)


def assess(**overrides):
    return assess_market(make_market(**overrides), THRESHOLDS, now=NOW)


class TestRejections:
    def test_zero_quote_market_is_rejected(self):
        assessment = assess(yes_bid=None, yes_ask=None)
        assert not assessment.is_eligible
        assert not assessment.has_nonzero_quotes
        assert REASON_NO_QUOTES in assessment.rejection_reasons

    def test_one_sided_quote_is_rejected_when_two_sided_required(self):
        assessment = assess(yes_bid=45, yes_ask=None)
        assert not assessment.is_eligible
        assert assessment.has_nonzero_quotes
        assert not assessment.has_two_sided_quote
        assert REASON_ONE_SIDED_QUOTE in assessment.rejection_reasons

    def test_one_sided_quote_allowed_when_requirement_disabled(self):
        thresholds = EligibilityThresholds(require_two_sided_quote=False)
        assessment = assess_market(
            make_market(yes_bid=45, yes_ask=None), thresholds, now=NOW
        )
        assert REASON_ONE_SIDED_QUOTE not in assessment.rejection_reasons

    def test_wide_spread_is_rejected(self):
        assessment = assess(yes_bid=30, yes_ask=70)  # 40c > 20c max
        assert not assessment.is_eligible
        assert not assessment.spread_ok
        assert REASON_SPREAD_TOO_WIDE in assessment.rejection_reasons

    def test_spread_at_threshold_is_allowed(self):
        assessment = assess(yes_bid=40, yes_ask=60)  # exactly 20c
        assert assessment.spread_ok
        assert REASON_SPREAD_TOO_WIDE not in assessment.rejection_reasons

    def test_low_liquidity_is_rejected(self):
        assessment = assess(liquidity=99)
        assert not assessment.is_eligible
        assert not assessment.liquidity_ok
        assert REASON_LIQUIDITY_BELOW_MIN in assessment.rejection_reasons

    def test_low_volume_is_rejected(self):
        assessment = assess(volume_24h=24)
        assert not assessment.is_eligible
        assert not assessment.volume_ok
        assert REASON_VOLUME_24H_BELOW_MIN in assessment.rejection_reasons

    def test_expiration_too_soon_is_rejected(self):
        assessment = assess(close_time=days_out(0.1))
        assert not assessment.is_eligible
        assert REASON_EXPIRES_TOO_SOON in assessment.rejection_reasons

    def test_expiration_too_far_is_rejected(self):
        assessment = assess(close_time=days_out(90))
        assert not assessment.is_eligible
        assert REASON_EXPIRES_TOO_FAR in assessment.rejection_reasons

    def test_missing_expiration_is_rejected(self):
        assessment = assess(close_time=None)
        assert not assessment.is_eligible
        assert assessment.expiration_days is None
        assert REASON_MISSING_EXPIRATION in assessment.rejection_reasons

    def test_rejection_reasons_accumulate(self):
        assessment = assess(
            yes_bid=None, yes_ask=None, liquidity=0, volume_24h=0, close_time=None
        )
        assert set(assessment.rejection_reasons) == {
            REASON_NO_QUOTES,
            REASON_LIQUIDITY_BELOW_MIN,
            REASON_VOLUME_24H_BELOW_MIN,
            REASON_MISSING_EXPIRATION,
        }


class TestEligibleAndFlags:
    def test_healthy_market_is_eligible_with_no_reasons(self):
        assessment = assess(close_time=days_out(7))
        assert assessment.is_eligible
        assert assessment.rejection_reasons == []
        assert assessment.has_two_sided_quote
        assert assessment.spread_ok and assessment.liquidity_ok
        assert assessment.volume_ok and assessment.expiration_ok
        assert assessment.expiration_days == 7.0

    def test_kxmve_parlay_market_is_flagged_and_rejected(self):
        """The exact MVP-002 failure mode: unquoted zero-liquidity parlay."""
        assessment = assess(
            ticker="KXMVECROSSCATEGORY-S2026-ABC",
            title="yes Spain advances,yes Croatia advances,yes Argentina advances",
            yes_bid=None,
            yes_ask=None,
            liquidity=0,
            volume_24h=0,
            close_time=days_out(18),
        )
        assert not assessment.is_eligible
        assert assessment.market_type_flags["multivariate"]
        assert assessment.market_type_flags["combo_title"]
        assert WARNING_PARLAY_LIKE in assessment.warnings
        assert REASON_NO_QUOTES in assessment.rejection_reasons

    def test_parlay_flag_alone_is_warning_not_rejection(self):
        assessment = assess(ticker="KXMVE-QUOTED-1", close_time=days_out(7))
        assert assessment.is_eligible
        assert WARNING_PARLAY_LIKE in assessment.warnings


class TestThresholdOverrides:
    def test_thresholds_can_be_overridden_directly(self):
        strict = EligibilityThresholds(min_liquidity=1_000_000)
        lax = EligibilityThresholds(min_liquidity=0)
        market = make_market(liquidity=500)
        assert not assess_market(market, strict, now=NOW).is_eligible
        assert assess_market(market, lax, now=NOW).is_eligible

    def test_thresholds_load_from_env_config(self, monkeypatch):
        monkeypatch.setenv("REQUIRE_TWO_SIDED_QUOTE", "false")
        monkeypatch.setenv("MIN_LIQUIDITY", "5000")
        monkeypatch.setenv("MIN_VOLUME_24H", "50")
        monkeypatch.setenv("MAX_SPREAD", "0.10")
        monkeypatch.setenv("MIN_DAYS_TO_EXPIRATION", "1.0")
        monkeypatch.setenv("MAX_DAYS_TO_EXPIRATION", "30")
        monkeypatch.setenv("EXCLUDE_ZERO_QUOTE_MARKETS", "false")

        thresholds = EligibilityThresholds.from_settings(Settings(_env_file=None))
        assert thresholds.require_two_sided_quote is False
        assert thresholds.exclude_zero_quote_markets is False
        assert thresholds.min_liquidity == 5000
        assert thresholds.min_volume_24h == 50
        assert thresholds.max_spread_cents == 10
        assert thresholds.min_days_to_expiration == 1.0
        assert thresholds.max_days_to_expiration == 30.0

    def test_spec_defaults(self):
        thresholds = EligibilityThresholds()
        assert thresholds.require_two_sided_quote is True
        assert thresholds.exclude_zero_quote_markets is True
        assert thresholds.min_liquidity == 100
        assert thresholds.min_volume_24h == 25
        assert thresholds.max_spread_cents == 20
        assert thresholds.min_days_to_expiration == 0.25
        assert thresholds.max_days_to_expiration == 45.0
