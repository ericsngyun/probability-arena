"""CRYPTO-RETROSPECT-002 tests: tape-backed cohort stratification.

Covers: --cohort filtering (all/tape-backed/derived-only), the tape-vs-derived
split, source-comparison label precedence (all six labels), the dilution
warning, data_source_mix + maturity-by-source math, provider-gap rate by
source, end-to-end stratification that surfaces a tape_only_hint hidden in the
all-window view, cohort validation, no persistence, no external calls, CLI
rendering, and no forbidden trading/execution vocabulary. In-memory SQLite.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from app import cli
from app.services.crypto_retrospect import (
    ALL_OUTCOMES,
    COHORT_CHOICES,
    DIMENSIONS,
    LABEL_GAP_DOMINATED,
    LABEL_NO_SEPARATION,
    LABEL_STRONG_SURVIVAL,
    LABEL_TOO_THIN,
    LABEL_WEAK,
    SOURCE_ALL_DILUTED,
    SOURCE_CONSISTENT,
    SOURCE_DERIVED_DOMINATES,
    SOURCE_TAPE_ONLY_HINT,
    SOURCE_TAPE_READABLE,
    SOURCE_TAPE_TOO_THIN,
    FeatureOutcomeRow,
    build_data_source_mix,
    build_retrospect_report,
    build_source_stratification,
    _filter_by_cohort,
    source_label,
)
from app.services.crypto_tape import CryptoLifecycleTapeRecorder, CryptoTapeConfig
from tests.test_crypto_tape_001 import (
    NOW,
    add_token,
    seed_full_token,
    session,  # fixture reuse
)

REPO = Path(__file__).resolve().parents[1]


def frow(tape_backed, boost="not_boosted", survived_1h=None, **outcome_over):
    """A FeatureOutcomeRow with every dimension bucket filled (so
    build_source_stratification never KeyErrors) — only `boost` varies."""
    buckets = {d: "absent" for d in DIMENSIONS}
    buckets["boost"] = boost
    outcomes = {o: None for o in ALL_OUTCOMES}
    outcomes["survived_1h"] = survived_1h
    outcomes.update(outcome_over)
    return FeatureOutcomeRow(
        token_address="tok" + "A" * 17, symbol="S", tape_backed=tape_backed,
        buckets=buckets, risk_reasons=[], missing_info=[], outcomes=outcomes,
    )


def interp(label, delta=None, outcome=None):
    d = {"label": label}
    if delta is not None:
        d["max_delta"] = delta
        d["driving_outcome"] = outcome
    return d


# --- cohort filtering --------------------------------------------------------------


class TestCohortFilter:
    def test_filter_splits_by_source(self):
        rows = [frow(True), frow(True), frow(False)]
        assert len(_filter_by_cohort(rows, "all")) == 3
        assert len(_filter_by_cohort(rows, "tape-backed")) == 2
        assert len(_filter_by_cohort(rows, "derived-only")) == 1

    def test_cohort_choices_constant(self):
        assert COHORT_CHOICES == ("all", "tape-backed", "derived-only")


# --- source label precedence -------------------------------------------------------


class TestSourceLabel:
    def test_tape_too_thin_when_tape_unreadable_and_all_unreadable(self):
        out = source_label(
            interp(LABEL_TOO_THIN), interp(LABEL_TOO_THIN), interp(LABEL_TOO_THIN),
            n_tape=5, n_derived=5,
        )
        assert out == SOURCE_TAPE_TOO_THIN

    def test_derived_dominates_when_only_all_readable_and_derived_bigger(self):
        out = source_label(
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),  # all readable
            interp(LABEL_TOO_THIN),                             # tape not readable
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            n_tape=10, n_derived=200,
        )
        assert out == SOURCE_DERIVED_DOMINATES

    def test_consistent_when_both_signal_same_outcome(self):
        out = source_label(
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            interp(LABEL_STRONG_SURVIVAL, 0.4, "survived_1h"),
            interp(LABEL_WEAK, 0.15, "survived_1h"),
            n_tape=50, n_derived=50,
        )
        assert out == SOURCE_CONSISTENT

    def test_all_diluted_when_signals_disagree(self):
        out = source_label(
            interp(LABEL_GAP_DOMINATED),
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            interp(LABEL_WEAK, 0.15, "liquidity_removed"),
            n_tape=50, n_derived=50,
        )
        assert out == SOURCE_ALL_DILUTED

    def test_all_diluted_when_derived_measured_but_flat(self):
        out = source_label(
            interp(LABEL_NO_SEPARATION, 0.02, "survived_1h"),
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            interp(LABEL_NO_SEPARATION, 0.03, "survived_1h"),  # readable, no signal
            n_tape=50, n_derived=200,
        )
        assert out == SOURCE_ALL_DILUTED

    def test_tape_only_hint_when_derived_too_thin(self):
        out = source_label(
            interp(LABEL_GAP_DOMINATED),
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            interp(LABEL_TOO_THIN),                            # can't cross-check
            n_tape=50, n_derived=200,
        )
        assert out == SOURCE_TAPE_ONLY_HINT

    def test_derived_dominates_when_tape_flat_derived_signal(self):
        out = source_label(
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            interp(LABEL_NO_SEPARATION, 0.02, "survived_1h"),  # tape readable, no signal
            interp(LABEL_STRONG_SURVIVAL, 0.5, "survived_1h"),
            n_tape=50, n_derived=200,
        )
        assert out == SOURCE_DERIVED_DOMINATES

    def test_tape_readable_when_neither_signals(self):
        out = source_label(
            interp(LABEL_NO_SEPARATION, 0.02, "survived_1h"),
            interp(LABEL_NO_SEPARATION, 0.03, "survived_1h"),
            interp(LABEL_NO_SEPARATION, 0.01, "survived_1h"),
            n_tape=50, n_derived=50,
        )
        assert out == SOURCE_TAPE_READABLE


# --- stratification + dilution -----------------------------------------------------


class TestStratification:
    def test_tape_signal_hidden_in_all_window_flags_dilution(self):
        # tape-backed: clean strong survival separator on boost
        rows = (
            [frow(True, "boosted", survived_1h=True) for _ in range(12)]
            + [frow(True, "not_boosted", survived_1h=False) for _ in range(12)]
            # derived-only: many immature boosted tokens dilute the all-window
            + [frow(False, "boosted", survived_1h=None) for _ in range(40)]
        )
        strat = {s["dimension"]: s for s in build_source_stratification(rows, top=3)}
        boost = strat["boost"]
        assert boost["tape_backed"]["label"] == LABEL_STRONG_SURVIVAL
        # all-window pushed below the gap floor by immature derived rows
        assert boost["all"]["label"] == LABEL_GAP_DOMINATED
        assert boost["diluted"] is True
        assert boost["source_label"] == SOURCE_TAPE_ONLY_HINT
        assert "read the tape-backed column" in boost["warning"]

    def test_no_dilution_when_all_window_keeps_signal(self):
        rows = (
            [frow(True, "boosted", survived_1h=True) for _ in range(12)]
            + [frow(True, "not_boosted", survived_1h=False) for _ in range(12)]
            + [frow(False, "boosted", survived_1h=True) for _ in range(12)]
            + [frow(False, "not_boosted", survived_1h=False) for _ in range(12)]
        )
        strat = {s["dimension"]: s for s in build_source_stratification(rows, top=3)}
        boost = strat["boost"]
        assert boost["diluted"] is False
        assert boost["source_label"] == SOURCE_CONSISTENT

    def test_thin_dimension_is_tape_too_thin(self):
        rows = [frow(True, "boosted", survived_1h=True) for _ in range(3)]
        strat = {s["dimension"]: s for s in build_source_stratification(rows, top=3)}
        assert strat["boost"]["source_label"] == SOURCE_TAPE_TOO_THIN
        assert strat["boost"]["diluted"] is False


# --- data source mix + maturity by source ------------------------------------------


class TestDataSourceMix:
    def test_counts_immature_and_maturity_by_source(self):
        rows = (
            [frow(True, survived_1h=True, survived_15m=True) for _ in range(3)]
            + [frow(True, survived_1h=None) for _ in range(2)]      # immature tape
            + [frow(False, survived_1h=None) for _ in range(5)]     # immature derived
        )
        mix = build_data_source_mix(rows)
        assert mix["tape_backed"] == 5
        assert mix["derived_only"] == 5
        assert mix["immature"] == 7                                 # 2 tape + 5 derived
        tb = mix["horizon_coverage_by_source"]["tape_backed"]
        assert tb["survived_1h"] == {"known": 3, "unknown": 2}
        assert tb["survived_15m"] == {"known": 3, "unknown": 2}
        dv = mix["horizon_coverage_by_source"]["derived_only"]
        assert dv["survived_1h"] == {"known": 0, "unknown": 5}

    def test_provider_gap_rate_by_source(self):
        rows = (
            [frow(True, provider_gap=True) for _ in range(3)]
            + [frow(True, provider_gap=False) for _ in range(1)]
            + [frow(False, provider_gap=True) for _ in range(2)]
        )
        mix = build_data_source_mix(rows)
        gap = mix["provider_gap_rate_by_source"]
        assert gap["tape_backed"] == 0.75   # 3/4
        assert gap["derived_only"] == 1.0   # 2/2
        assert gap["all"] == round(5 / 6, 4)


# --- report integration (DB) -------------------------------------------------------


class TestReportCohort:
    def test_cohort_filters_headline_but_keeps_full_source_mix(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        CryptoLifecycleTapeRecorder(
            config=CryptoTapeConfig(chain="solana")
        ).run_once(session)  # token becomes tape-backed

        r_all = build_retrospect_report(session, hours=48, cohort="all")
        assert r_all["cohort"] == "all"
        assert r_all["tokens_analyzed"] == 1
        assert r_all["window_tokens"] == 1
        assert r_all["data_source_mix"]["tape_backed"] == 1
        assert "source_stratification" in r_all

        r_tape = build_retrospect_report(session, hours=48, cohort="tape-backed")
        assert r_tape["tokens_analyzed"] == 1
        r_derived = build_retrospect_report(session, hours=48, cohort="derived-only")
        assert r_derived["tokens_analyzed"] == 0
        # full-window source mix is unchanged regardless of the lens
        assert r_derived["data_source_mix"]["tape_backed"] == 1

    def test_derived_only_token_when_no_tape_run(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = build_retrospect_report(session, hours=48, cohort="all")
        assert r["data_source_mix"]["tape_backed"] == 0
        assert r["data_source_mix"]["derived_only"] == 1
        assert build_retrospect_report(
            session, hours=48, cohort="tape-backed"
        )["tokens_analyzed"] == 0

    def test_bad_cohort_raises(self, session):
        with pytest.raises(ValueError):
            build_retrospect_report(session, hours=48, cohort="nonsense")

    def test_persists_nothing(self, session):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        session.flush()
        build_retrospect_report(session, hours=48, cohort="all")
        assert not session.new
        assert not session.dirty


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    async def test_cli_renders_stratification_and_cohort(self, session, capsys):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        n = await cli.crypto_retrospect_report(
            hours=48, top=5, cohort="all", session=session
        )
        out = capsys.readouterr().out
        assert n == 1
        assert "data source mix:" in out
        assert "horizon maturity (known/unknown) by source:" in out
        assert "source stratification" in out
        assert "cohort=all" in out

    async def test_cli_tape_backed_cohort(self, session, capsys):
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        await cli.crypto_retrospect_report(
            hours=48, cohort="tape-backed", session=session
        )
        out = capsys.readouterr().out
        assert "cohort=tape-backed" in out

    def test_main_wires_cohort_choices(self):
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
        choices = holder["parser"]._subparsers._group_actions[0].choices
        retro = choices["crypto-retrospect-report"]
        cohort_action = next(
            a for a in retro._actions if a.dest == "cohort"
        )
        assert set(cohort_action.choices) == {"all", "tape-backed", "derived-only"}


# --- safety ------------------------------------------------------------------------


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

    async def test_no_network_even_with_broken_httpx(self, session, monkeypatch):
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("retrospect must not construct an HTTP client")

        monkeypatch.setattr(httpx, "AsyncClient", explode)
        monkeypatch.setattr(httpx, "Client", explode)
        seed_full_token(session, first_seen=NOW - timedelta(hours=2))
        r = build_retrospect_report(session, hours=48, cohort="all")
        assert r["window_tokens"] == 1
