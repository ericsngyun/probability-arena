"""CRYPTO-RETROSPECT-001 tests: read-only retrospective feature/outcome
separation analysis.

Covers: pure cohort bucketing, feature/outcome row assembly over persisted
rows (tape-backed and derived-only), immature-outcome handling (unknown never
enters a rate), provider-gap dominance, conservative separation-label logic,
report structure + CLI rendering, strict no-persistence (nothing added to
the session, no row counts change), no network calls, and no forbidden
trading/execution vocabulary. In-memory SQLite; no network anywhere.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.models import (
    CryptoPriceTick,
    CryptoToken,
    CryptoTokenBirthEvent,
    CryptoTokenLifecycleSnapshot,
    CryptoTokenRiskAssessment,
    CryptoTokenSurvivalOutcome,
)
from app.services.crypto_retrospect import (
    LABEL_GAP_DOMINATED,
    LABEL_NO_SEPARATION,
    LABEL_STRONG_RISK,
    LABEL_STRONG_SURVIVAL,
    LABEL_TOO_THIN,
    LABEL_WEAK,
    MIN_COHORT_SAMPLES,
    MIN_MEASURABLE,
    CryptoRetrospectService,
    FeatureOutcomeRow,
    bucket_attention,
    bucket_boost,
    bucket_concentration,
    bucket_liquidity,
    bucket_social,
    bucket_volume_to_liquidity,
    build_retrospect_report,
    cohort_stats,
    interpret_dimension,
)
from app.services.crypto_tape import CryptoLifecycleTapeRecorder, CryptoTapeConfig
from tests.test_crypto_tape_001 import (
    NOW,
    add_token,
    seed_full_token,
    session,  # fixture reuse
)

REPO = Path(__file__).resolve().parents[1]


def service() -> CryptoRetrospectService:
    return CryptoRetrospectService(
        recorder=CryptoLifecycleTapeRecorder(config=CryptoTapeConfig(chain="solana"))
    )


# --- pure bucketing ---------------------------------------------------------------


class TestBuckets:
    def test_concentration_buckets(self):
        assert bucket_concentration(None, 20) == "absent"
        assert bucket_concentration(5, 20) == "low"
        assert bucket_concentration(10, 20) == "elevated"
        assert bucket_concentration(20, 20) == "flagged"
        assert bucket_concentration(85, 20) == "flagged"

    def test_liquidity_buckets(self):
        assert bucket_liquidity(None) == "absent"
        assert bucket_liquidity(1_000) == "<5k"
        assert bucket_liquidity(10_000) == "5k-25k"
        assert bucket_liquidity(50_000) == "25k-100k"
        assert bucket_liquidity(500_000) == ">=100k"

    def test_volume_to_liquidity_buckets(self):
        assert bucket_volume_to_liquidity(None) == "absent"
        assert bucket_volume_to_liquidity(0.1) == "quiet(<0.5x)"
        assert bucket_volume_to_liquidity(1.0) == "active(0.5-2x)"
        assert bucket_volume_to_liquidity(5.0) == "hot(2-20x)"
        assert bucket_volume_to_liquidity(25.0) == "suspect(>=20x)"

    def test_attention_boost_social(self):
        assert bucket_attention(None) == "absent"
        assert bucket_attention(0.2) == "low(<0.4)"
        assert bucket_attention(0.5) == "mid(0.4-0.7)"
        assert bucket_attention(0.9) == "high(>=0.7)"
        assert bucket_boost(None) == "absent"
        assert bucket_boost(0) == "not_boosted"
        assert bucket_boost(40) == "boosted"
        assert bucket_social(None) == "unknown"
        assert bucket_social(True) == "social_present"
        assert bucket_social(False) == "social_missing"


# --- row assembly -----------------------------------------------------------------


class TestRows:
    def test_features_and_outcomes_for_seeded_token(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        rows, truncated = service().rows(session, hours=48)
        assert len(rows) == 1 and truncated is False
        row = rows[0]
        assert row.tape_backed is False  # no tape run happened
        b = row.buckets
        assert b["top10_concentration"] == "flagged"      # 22 >= 20
        assert b["sniper_concentration"] == "low"         # 3 < 10
        assert b["risk_level"] == "low"
        assert b["liquidity"] == "5k-25k"
        assert b["volume_to_liquidity"] == "active(0.5-2x)"
        assert b["boost"] == "boosted"
        assert b["attention"] == "mid(0.4-0.7)"
        assert b["social_metadata"] == "social_present"
        assert b["launch_venue"] == "amm_pool"
        assert b["provider_coverage"] == "provider_backed"
        # 2h-old token: 15m horizon has no later tick -> unknown, honest
        assert row.outcomes["survived_24h"] is None
        assert row.outcomes["provider_gap"] is True

    def test_tape_backed_flag_after_real_tape_run(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        CryptoLifecycleTapeRecorder(
            config=CryptoTapeConfig(chain="solana")
        ).run_once(session)
        rows, _ = service().rows(session, hours=48)
        assert rows[0].tape_backed is True

    def test_missing_sources_bucket_as_absent(self, session):
        add_token(session, first_seen=NOW - timedelta(hours=2), metadata={})
        rows, _ = service().rows(session, hours=48)
        b = rows[0].buckets
        assert b["top10_concentration"] == "absent"
        assert b["liquidity"] == "absent"
        assert b["provider_coverage"] == "no_provider_read"
        assert b["launch_venue"] == "unknown"
        assert "initial_market_state" in rows[0].missing_info


# --- cohort stats + immature handling -----------------------------------------------


def make_rows(count, prefix="t", **outcomes) -> list[FeatureOutcomeRow]:
    return [
        FeatureOutcomeRow(
            token_address=f"{prefix}{i}" + "A" * 24, symbol="S",
            tape_backed=False, outcomes=dict(outcomes),
        )
        for i in range(count)
    ]


class TestCohortStats:
    def test_unknown_outcomes_never_enter_rates(self):
        group = make_rows(15, survived_1h=None, liquidity_removed=None)
        stats = cohort_stats("c", group)
        assert stats["outcomes"]["survived_1h"]["unknown"] == 15
        assert stats["outcomes"]["survived_1h"]["rate"] is None

    def test_rate_needs_min_measurable(self):
        group = (
            make_rows(MIN_MEASURABLE - 1, survived_1h=True)
            + make_rows(10, survived_1h=None)
        )
        stats = cohort_stats("c", group)
        assert stats["outcomes"]["survived_1h"]["rate"] is None  # 5 measured < 6

    def test_rate_over_measured_only(self):
        group = (
            make_rows(6, survived_1h=True)
            + make_rows(2, prefix="u", survived_1h=False)
            + make_rows(10, prefix="v", survived_1h=None)
        )
        stats = cohort_stats("c", group)
        assert stats["outcomes"]["survived_1h"]["rate"] == 0.75  # 6/8, unknown excluded

    def test_too_thin_label(self):
        stats = cohort_stats("c", make_rows(MIN_COHORT_SAMPLES - 1, survived_1h=True))
        assert stats["label"] == LABEL_TOO_THIN


# --- separation interpretation -------------------------------------------------------


def cohorts_for(*groups) -> list[dict]:
    return [cohort_stats(name, rows) for name, rows in groups]


class TestInterpretation:
    def test_too_thin_when_under_two_measured_cohorts(self):
        cohorts = cohorts_for(("a", make_rows(5, survived_1h=True)),
                              ("b", make_rows(4, survived_1h=False)))
        assert interpret_dimension(cohorts)["label"] == LABEL_TOO_THIN

    def test_strong_risk_separator(self):
        cohorts = cohorts_for(
            ("flagged", make_rows(15, liquidity_removed=True, survived_1h=True)),
            ("low", make_rows(15, prefix="b", liquidity_removed=False, survived_1h=True)),
        )
        out = interpret_dimension(cohorts)
        assert out["label"] == LABEL_STRONG_RISK
        assert out["driving_outcome"] == "liquidity_removed"
        assert out["max_delta"] == 1.0

    def test_strong_survival_separator(self):
        cohorts = cohorts_for(
            ("a", make_rows(15, survived_1h=True, liquidity_removed=False)),
            ("b", make_rows(15, prefix="b", survived_1h=False, liquidity_removed=False)),
        )
        out = interpret_dimension(cohorts)
        assert out["label"] == LABEL_STRONG_SURVIVAL
        assert out["driving_outcome"] == "survived_1h"

    def test_weak_separator(self):
        a = make_rows(12, survived_1h=True, liquidity_removed=False) + make_rows(
            3, prefix="x", survived_1h=False, liquidity_removed=False
        )
        b = make_rows(10, prefix="b", survived_1h=True, liquidity_removed=False) + make_rows(
            5, prefix="y", survived_1h=False, liquidity_removed=False
        )
        out = interpret_dimension(cohorts_for(("a", a), ("b", b)))
        assert out["label"] == LABEL_WEAK  # delta 0.8 - 0.6667 = 0.1333

    def test_no_separation_when_rates_match(self):
        cohorts = cohorts_for(
            ("a", make_rows(15, survived_1h=True, liquidity_removed=False)),
            ("b", make_rows(15, prefix="b", survived_1h=True, liquidity_removed=False)),
        )
        assert interpret_dimension(cohorts)["label"] == LABEL_NO_SEPARATION

    def test_provider_gap_dominates_when_mostly_unmeasurable(self):
        a = make_rows(2, survived_1h=True) + make_rows(13, prefix="x", survived_1h=None)
        b = make_rows(2, prefix="b", survived_1h=False) + make_rows(
            13, prefix="y", survived_1h=None
        )
        out = interpret_dimension(cohorts_for(("a", a), ("b", b)))
        assert out["label"] == LABEL_GAP_DOMINATED
        assert "collect more tape" in out["basis"]


# --- report + no-persistence + no-network ---------------------------------------------


def all_row_counts(session) -> dict:
    counts = {}
    for model in (CryptoToken, CryptoPriceTick, CryptoTokenRiskAssessment,
                  CryptoTokenBirthEvent, CryptoTokenLifecycleSnapshot,
                  CryptoTokenSurvivalOutcome):
        counts[model.__tablename__] = session.query(model).count()
    return counts


class TestReport:
    def test_report_structure_on_seeded_data(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = build_retrospect_report(session, hours=48, top=5)
        assert r["tokens_analyzed"] == 1
        assert r["derived_only_tokens"] == 1
        assert r["outcome_totals"]["provider_gap"]["true"] == 1
        dims = {d["dimension"] for d in r["dimensions"]}
        assert {"top10_concentration", "risk_level", "liquidity", "boost",
                "launch_venue", "provider_coverage", "risk_reason",
                "missing_info"} <= dims
        # n=1 everywhere -> every dimension honestly too_thin
        for d in r["dimensions"]:
            assert d["interpretation"]["label"] == LABEL_TOO_THIN
        assert "never advice" in r["disclaimer"]
        assert r["thresholds"]["min_cohort_samples"] == MIN_COHORT_SAMPLES

    def test_empty_report(self, session):
        r = build_retrospect_report(session)
        assert r["tokens_analyzed"] == 0
        assert r["best_separators"] == []

    def test_persists_nothing(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        session.flush()
        before = all_row_counts(session)
        build_retrospect_report(session, hours=48)
        assert not session.new, "retrospect must never add rows to the session"
        assert not session.dirty, "retrospect must never modify rows"
        assert all_row_counts(session) == before

    async def test_cli_renders(self, session, capsys):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        n = await cli.crypto_retrospect_report(session=session, hours=48, top=5)
        out = capsys.readouterr().out
        assert n == 1
        assert "measurement only, never advice" in out
        assert "outcome totals" in out
        assert "too_thin" in out
        assert "disclaimer:" in out

    def test_main_wires_command(self):
        import argparse

        holder = {}
        original = argparse.ArgumentParser.parse_args

        def fake_parse(self, *a, **k):
            holder["parser"] = self
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = fake_parse
        try:
            with pytest.raises(SystemExit):
                cli.main([])
        finally:
            argparse.ArgumentParser.parse_args = original
        actions = holder["parser"]._subparsers._group_actions[0].choices
        assert "crypto-retrospect-report" in actions


class TestSafety:
    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "crypto_retrospect.py").read_text()
        toks = [
            t.string.lower()
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "submit_order", "create_order", "wallet",
                    "private_key", "recommend", "execute_trade", "swap",
                    "jupiter", "sign_transaction", "pnl", "profit", "buy",
                    "sell", "arbitrage", "entry_price", "stop_loss"):
            assert bad not in code, bad

    def test_no_direct_network_imports(self):
        src = (REPO / "app" / "services" / "crypto_retrospect.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket",
                    "adapters"):
            assert net not in src

    def test_report_makes_no_network_calls_even_with_broken_httpx(
        self, session, monkeypatch
    ):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("retrospect must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = build_retrospect_report(session, hours=48)
        assert r["tokens_analyzed"] == 1
