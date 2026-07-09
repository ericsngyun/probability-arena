"""EDGE-SELECTION-001 tests: pre-registered read-only validation protocol.

Registry freeze (exactly the pre-registered policies, no arbitrary additions),
window classification (discovery/validation/mixed), success and failure gates,
concentration guard, negative-control semantics, discovery-vs-validation
labeling, report rendering (MVP-005B line always printed), no persistence, no
network, no forbidden vocabulary. In-memory SQLite; nothing live is touched.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from app.services.edge_filter_shadow import POLICIES
from app.services.edge_selection import (
    PREREG_DOC,
    PREREG_LOCKED_AT,
    PREREGISTERED,
    ROLE_BASELINE,
    ROLE_CANDIDATE,
    ROLE_NEGATIVE_CONTROL,
    STATUS_CONTROL_ANOMALY,
    STATUS_CONTROL_CONSISTENT,
    STATUS_DISCOVERY_ONLY,
    STATUS_FAILING,
    STATUS_INCONCLUSIVE,
    STATUS_INSUFFICIENT,
    STATUS_SAMPLE_COLLAPSED,
    STATUS_VALIDATED,
    WINDOW_DISCOVERY,
    WINDOW_MIXED,
    WINDOW_VALIDATION,
    EdgeSelectionValidationReportService,
    classify_window,
    evaluate_gates,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


# --- registry freeze ---------------------------------------------------------------


class TestRegistryFreeze:
    def test_exactly_the_preregistered_policies(self):
        names = [name for name, _, _ in PREREGISTERED]
        assert names == [
            "baseline_all_watchlist",
            "require_gap_follows_move_totals_only",
            "require_gap_follows_move_exclude_spreads",
            "gap_follows_move_and_high_liquidity",
            "gap_follows_move_and_tight_spread",
            "total_only",
            "exclude_spread_markets",
            "spread_only",
        ]

    def test_roles_and_aliases_frozen(self):
        by_name = {n: (role, alias) for n, role, alias in PREREGISTERED}
        assert by_name["baseline_all_watchlist"][0] == ROLE_BASELINE
        assert by_name["spread_only"][0] == ROLE_NEGATIVE_CONTROL
        assert sum(1 for _, r, _ in PREREGISTERED if r == ROLE_CANDIDATE) == 6
        assert by_name["total_only"][1] == "totals_only"
        assert by_name["exclude_spread_markets"][1] == "exclude_spreads"

    def test_every_preregistered_policy_exists_in_filter_module(self):
        # no new predicates were invented for this milestone
        filter_names = {name for name, _ in POLICIES}
        for name, _, _ in PREREGISTERED:
            assert name in filter_names

    def test_lock_and_doc_constants(self):
        assert PREREG_LOCKED_AT == datetime(2026, 7, 9, 19, 0, tzinfo=timezone.utc)
        assert (REPO / PREREG_DOC).exists()


# --- window classification ------------------------------------------------------------


class TestClassifyWindow:
    LOCK = datetime(2026, 7, 9, 19, 0, tzinfo=timezone.utc)

    def test_discovery(self):
        assert classify_window(
            self.LOCK - timedelta(hours=48), self.LOCK - timedelta(hours=1), self.LOCK
        ) == WINDOW_DISCOVERY

    def test_validation(self):
        assert classify_window(
            self.LOCK + timedelta(hours=1), self.LOCK + timedelta(hours=25), self.LOCK
        ) == WINDOW_VALIDATION

    def test_validation_starting_exactly_at_lock(self):
        assert classify_window(
            self.LOCK, self.LOCK + timedelta(hours=24), self.LOCK
        ) == WINDOW_VALIDATION

    def test_mixed_straddles_lock(self):
        assert classify_window(
            self.LOCK - timedelta(hours=12), self.LOCK + timedelta(hours=12), self.LOCK
        ) == WINDOW_MIXED


# --- gate evaluation (pure) ----------------------------------------------------------


def summary(n=100, toward=0.60, closure=0.20, ticker=0.10, game=0.20):
    return {
        "final_n": n,
        "follow_through": {
            "60m": {"moved_toward_rate": toward, "mean_gap_closure_pct": closure}
        },
        "max_ticker_share": ticker,
        "max_game_share": game,
    }


class TestGates:
    def test_validated_shadow_on_validation_window(self):
        out = evaluate_gates(summary(), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_VALIDATED
        assert "human acceptance" in out["status_reason"]

    def test_preferred_n_noted_when_between_75_and_150(self):
        out = evaluate_gates(summary(n=80), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_VALIDATED
        assert "preferred" in out["status_reason"]
        assert out["gates"]["sample_n_ge_150_preferred"] is False

    def test_discovery_window_can_never_validate(self):
        out = evaluate_gates(summary(n=200), WINDOW_DISCOVERY, ROLE_CANDIDATE)
        assert out["status"] == STATUS_DISCOVERY_ONLY
        assert out["gates"]["out_of_sample_window"] is False

    def test_mixed_window_cannot_validate(self):
        out = evaluate_gates(summary(n=200), WINDOW_MIXED, ROLE_CANDIDATE)
        assert out["status"] == STATUS_DISCOVERY_ONLY

    def test_failure_gate_toward_below_floor(self):
        out = evaluate_gates(summary(toward=0.45), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_FAILING
        assert any("toward_60m" in r for r in out["failure_reasons"])

    def test_failure_gate_negative_closure(self):
        out = evaluate_gates(
            summary(toward=0.56, closure=-0.05), WINDOW_VALIDATION, ROLE_CANDIDATE
        )
        assert out["status"] == STATUS_FAILING
        assert any("negative" in r for r in out["failure_reasons"])

    def test_concentration_guard_single_ticker(self):
        out = evaluate_gates(summary(ticker=0.40), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_FAILING
        assert out["gates"]["max_ticker_share_le_0_34"] is False

    def test_concentration_guard_single_game(self):
        out = evaluate_gates(summary(game=0.60), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_FAILING

    def test_sample_collapsed(self):
        out = evaluate_gates(summary(n=5), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_SAMPLE_COLLAPSED

    def test_insufficient_sample_not_failing(self):
        out = evaluate_gates(summary(n=40), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_INSUFFICIENT

    def test_inconclusive_between_floor_and_bar(self):
        out = evaluate_gates(summary(toward=0.52), WINDOW_VALIDATION, ROLE_CANDIDATE)
        assert out["status"] == STATUS_INCONCLUSIVE

    def test_negative_control_consistent_when_adverse(self):
        out = evaluate_gates(
            summary(toward=0.20, closure=-0.50), WINDOW_VALIDATION,
            ROLE_NEGATIVE_CONTROL,
        )
        assert out["status"] == STATUS_CONTROL_CONSISTENT

    def test_negative_control_anomaly_when_non_adverse(self):
        out = evaluate_gates(
            summary(toward=0.58, closure=0.10), WINDOW_VALIDATION,
            ROLE_NEGATIVE_CONTROL,
        )
        assert out["status"] == STATUS_CONTROL_ANOMALY
        assert "suspicion" in out["status_reason"]

    def test_baseline_is_reference_only(self):
        out = evaluate_gates(summary(), WINDOW_VALIDATION, ROLE_BASELINE)
        assert out["status"] == "baseline"


# --- end-to-end over DB ------------------------------------------------------------


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def tick(session, ticker, *, at, mid, spread=1, liquidity=1_000_000):
    session.add(MarketPriceTick(
        market_ticker=ticker, observed_at=at,
        yes_bid=int(mid * 100) - 1, yes_ask=int(mid * 100) + 1, midpoint=mid,
        spread=spread, volume_24h=100, liquidity_proxy=liquidity, created_at=at,
    ))


def seed(session, ticker, *, gap=0.10, midpoint=0.50, minutes_ago=90,
         later_mid=None, pre_mid=None, market_type="total", spread=1):
    created = NOW - timedelta(minutes=minutes_ago)
    f = MarketForecastRecord(
        market_ticker=ticker, forecaster_name="baseball_evidence",
        forecaster_version="v1", prompt_version="v1",
        estimated_probability=midpoint + gap, confidence=0.62,
        evidence_depth="source_backed", forecast_risk="medium",
        created_at=created - timedelta(seconds=60),
    )
    session.add(f)
    session.flush()
    sig = OpportunitySignal(
        market_ticker=ticker, signal_type="price_move_threshold",
        signal_status="forecast_refreshed",
        observed_at=created - timedelta(minutes=2), reason="seeded",
        created_at=created - timedelta(minutes=2),
    )
    session.add(sig)
    session.flush()
    session.add(EdgePrecheckSnapshot(
        market_ticker=ticker, signal_id=sig.id, forecast_id=f.id,
        forecaster_name="baseball_evidence", evidence_depth="source_backed",
        forecast_probability=midpoint + gap, forecast_confidence=0.62,
        market_midpoint=midpoint, spread_cents=spread,
        liquidity_proxy_cents=1_000_000, probability_gap=gap,
        abs_probability_gap=abs(gap), status="watchlist",
        invalidation_reasons=[], persistence_count=1,
        forecast_age_seconds=60, market_snapshot_age_seconds=10,
        tags=["domain:sports_baseball", f"market_type:{market_type}"],
        created_at=created,
    ))
    if pre_mid is not None:
        tick(session, ticker, at=created - timedelta(minutes=8), mid=pre_mid)
    if later_mid is not None:
        tick(session, ticker, at=created + timedelta(minutes=5), mid=later_mid)
    session.commit()


def seed_strong_totals(session, count):
    """`count` follows-move total rows across distinct games, all toward."""
    for i in range(count):
        seed(session, f"KXMLBTOTAL-G{i:03d}AAA-7",
             pre_mid=0.44, later_mid=0.56)   # pre-move +0.06 follows gap +0.10


def build(session, **kw):
    return EdgeSelectionValidationReportService().build(session, **kw)


def policy(report, name):
    return next(p for p in report["policies"] if p["name"] == name)


class TestEndToEnd:
    def test_only_preregistered_policies_reported(self, session):
        seed_strong_totals(session, 3)
        r = build(session)
        assert [p["name"] for p in r["policies"]] == [n for n, _, _ in PREREGISTERED]

    def test_validated_shadow_on_forced_validation_window(self, session):
        seed_strong_totals(session, 80)
        r = build(session, since=NOW - timedelta(hours=2), lock=NOW - timedelta(hours=3))
        assert r["window"]["type"] == WINDOW_VALIDATION
        p = policy(r, "require_gap_follows_move_totals_only")
        assert p["final_n"] == 80
        assert p["status"] == STATUS_VALIDATED
        assert "require_gap_follows_move_totals_only" in r["validated_shadow_policies"]

    def test_same_data_on_discovery_window_is_not_validated(self, session):
        seed_strong_totals(session, 80)
        r = build(session, since=NOW - timedelta(hours=2), lock=NOW + timedelta(hours=1))
        assert r["window"]["type"] == WINDOW_DISCOVERY
        assert policy(r, "require_gap_follows_move_totals_only")["status"] == (
            STATUS_DISCOVERY_ONLY
        )
        assert r["validated_shadow_policies"] == []

    def test_pre_and_post_lock_row_counts(self, session):
        seed(session, "KXMLBTOTAL-GAAA-7", minutes_ago=240,
             pre_mid=0.44, later_mid=0.56)
        seed(session, "KXMLBTOTAL-GBBB-7", minutes_ago=30,
             pre_mid=0.44, later_mid=0.56)
        r = build(session, hours=12, lock=NOW - timedelta(hours=2))
        assert r["window"]["type"] == WINDOW_MIXED
        assert r["window"]["rows_pre_lock"] == 1
        assert r["window"]["rows_post_lock"] == 1

    def test_negative_control_consistency_flag(self, session):
        # spreads adverse (closure negative), totals fine
        for i in range(15):
            seed(session, f"KXMLBSPREAD-S{i:03d}AAA-2", market_type="spread",
                 pre_mid=0.56, later_mid=0.42)
        r = build(session, since=NOW - timedelta(hours=2), lock=NOW - timedelta(hours=3))
        assert policy(r, "spread_only")["status"] == STATUS_CONTROL_CONSISTENT
        assert r["negative_control_consistent"] is True

    def test_failing_candidate_on_validation_window(self, session):
        # follows-move totals that all continued AWAY (negative closure)
        for i in range(15):
            seed(session, f"KXMLBTOTAL-G{i:03d}AAA-7", pre_mid=0.44, later_mid=0.42)
        r = build(session, since=NOW - timedelta(hours=2), lock=NOW - timedelta(hours=3))
        p = policy(r, "require_gap_follows_move_totals_only")
        assert p["status"] == STATUS_FAILING

    def test_mvp_005b_note_always_present(self, session):
        r = build(session)   # even on an empty window
        assert "MVP-005B remains blocked unless explicit human acceptance" in (
            r["mvp_005b_note"]
        )
        assert "Overfitting" in r["overfitting_note"] or "overfitting" in (
            r["overfitting_note"].lower()
        )


# --- CLI ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_parses_hours_since_until(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 1

        monkeypatch.setattr(cli, "edge_selection_validation_report", fake)
        rc = cli.main([
            "edge-selection-validation-report", "--hours", "48",
            "--since", "2026-07-09T19:00:00+00:00", "--until", "2026-07-10T19:00:00+00:00",
        ])
        assert rc == 0
        assert captured == {
            "hours": 48,
            "since": "2026-07-09T19:00:00+00:00",
            "until": "2026-07-10T19:00:00+00:00",
        }

    def test_cli_renders_with_mvp_line(self, session, capsys):
        seed_strong_totals(session, 3)
        n = asyncio.run(cli.edge_selection_validation_report(session=session))
        out = capsys.readouterr().out
        assert n == 3
        assert "MVP-005B remains blocked unless explicit human acceptance" in out
        assert "pre-registered" in out
        assert "spread_only" in out
        assert "overfitting risk" in out.lower()

    def test_cli_non_validation_window_warns(self, session, capsys):
        seed_strong_totals(session, 2)
        # hours window straddling any realistic lock start => discovery or mixed
        asyncio.run(cli.edge_selection_validation_report(hours=24, session=session))
        out = capsys.readouterr().out
        if "type=VALIDATION" not in out:
            assert "this window cannot" in out

    def test_cli_empty_window(self, session, capsys):
        n = asyncio.run(cli.edge_selection_validation_report(session=session))
        assert n == 0
        assert "population=0" in capsys.readouterr().out


# --- safety --------------------------------------------------------------------------


class TestSafety:
    def test_persists_nothing(self, session):
        seed_strong_totals(session, 3)
        import sqlalchemy

        tables = ("edge_precheck_snapshots", "market_price_ticks", "market_forecasts")
        before = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        build(session)
        session.commit()
        after = {
            t: session.execute(sqlalchemy.text(f"select count(*) from {t}")).scalar()
            for t in tables
        }
        assert before == after

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "edge_selection.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "jupiter", "recommend_trade",
                    "execute_trade", "execution"):
            assert bad not in code

    def test_no_live_network(self):
        src = (REPO / "app" / "services" / "edge_selection.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_prereg_doc_states_boundaries(self):
        doc = (REPO / PREREG_DOC).read_text()
        assert "MVP-005B remains blocked" in doc
        assert "validation protocol only" in doc
        assert "negative control" in doc.lower()
