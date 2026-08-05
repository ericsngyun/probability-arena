"""OUTCOME-SYNC-COVERAGE-001 — outcome-label integrity tests.

Most of these assert that something is REFUSED or PRESERVED. The milestone's
risk is not that we fail to score; it is that we manufacture a label. A missing
outcome must never become a loss, a closed market must never become a resolved
market, and an unrecognized provider status must never become yes or no.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db import Base
from app.models import (
    ForecastScoreRecord,
    MarketOpsRun,
    Market,
    MarketForecastRecord,
    MarketOutcomeRecord,
    MarketResearchPacket,
)
from app.services import outcome_backfill as ob
from app.services import outcome_coverage as oc
from app.services.calibration import CalibrationService
from app.services.outcomes import OutcomeService

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def repair_enabled(monkeypatch):
    """OUTCOME-SYNC-COVERAGE-001 is default-OFF so it can land dark.

    These tests exercise the repaired behavior, so they turn it on explicitly.
    `TestFlagGating` below asserts the OFF path separately — that the deployed
    prefix selections survive byte-for-byte until someone flips the flag.
    """
    from app.config import get_settings

    monkeypatch.setenv("ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coverage.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def mk_market(db, ticker, *, close_hours_ago=48.0, status="active"):
    m = Market(
        ticker=ticker, event_ticker="EV", title=ticker, category="c",
        status=status, close_time=NOW - timedelta(hours=close_hours_ago),
        first_seen_at=NOW - timedelta(days=30), last_seen_at=NOW,
    )
    db.add(m)
    db.commit()
    return m


def mk_forecast(db, ticker, *, probability=0.6, domain="sports_baseball",
                created_hours_ago=72.0, name="rule", version="v1"):
    packet = MarketResearchPacket(
        market_ticker=ticker, collector_name="test", domain=domain,
        research_completeness_score=0.9, research_risk="low",
        created_at=NOW - timedelta(days=3))
    db.add(packet)
    db.flush()
    f = MarketForecastRecord(
        market_ticker=ticker, estimated_probability=probability,
        forecaster_name=name, forecaster_version=version, confidence=0.8,
        evidence_depth="shallow", forecast_risk="low",
        research_packet_id=packet.id,
        created_at=NOW - timedelta(hours=created_hours_ago),
    )
    db.add(f)
    db.commit()
    return f


def mk_outcome(db, ticker, *, status="settled", side="yes", prob=None,
               close_hours_ago=48.0):
    if prob is None and side in ("yes", "no"):
        prob = 1.0 if side == "yes" else 0.0
    row = MarketOutcomeRecord(
        market_ticker=ticker, outcome_status=status, winning_side=side,
        resolved_probability=prob, close_time=NOW - timedelta(hours=close_hours_ago),
        source="kalshi_rest", created_at=NOW - timedelta(hours=1),
    )
    db.add(row)
    db.commit()
    return row


# --- Gate 2: denominator -------------------------------------------------------


class TestMaturedDenominator:
    def test_denominator_does_not_depend_on_outcome_presence(self, db):
        """The whole point: a forecast is matured because its market closed,
        NOT because we happen to have synced it."""
        for i in range(4):
            mk_market(db, f"T-{i}")
            mk_forecast(db, f"T-{i}")
        mk_outcome(db, "T-0")  # only one has an outcome

        r = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        assert r["funnel"]["matured_eligible"] == 4
        assert r["funnel"]["outcome_row_present"] == 1
        assert r["funnel"]["missing_outcome"] == 3

    def test_funnel_reconciles_exactly_and_is_monotonic(self, db):
        mk_market(db, "A"); mk_forecast(db, "A"); mk_outcome(db, "A", side="yes")
        mk_market(db, "B"); mk_forecast(db, "B"); mk_outcome(db, "B", status="closed", side=None)
        mk_market(db, "C"); mk_forecast(db, "C")
        mk_market(db, "D"); mk_forecast(db, "D"); mk_outcome(db, "D", status="canceled", side="void")

        f = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()["funnel"]
        assert f["all_forecasts"] >= f["matured_eligible"]
        assert f["matured_eligible"] >= f["outcome_row_present"]
        assert f["matured_eligible"] == f["settled_yes_no"] + f["missing_outcome"]
        assert f["settled_yes_no"] == 1

    def test_immature_market_is_not_in_the_denominator(self, db):
        """Inside the settlement grace, a missing outcome is latency, not a gap."""
        mk_market(db, "FRESH", close_hours_ago=0.1)
        mk_forecast(db, "FRESH")
        f = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()["funnel"]
        assert f["matured_eligible"] == 0

    def test_scoring_target_is_structurally_guaranteed(self, db):
        """"Its scoring target is known" is enforced by the schema, not by the
        denominator: estimated_probability is NOT NULL, so no forecast can
        reach the population without one. Asserted rather than assumed."""
        import app.models as m
        from app.db import Base

        col = Base.metadata.tables["market_forecasts"].c["estimated_probability"]
        assert col.nullable is False
        rows = oc.load_coverage_rows(db, now=NOW)
        assert all(r.forecast_id for r in rows)


# --- Gate 3: taxonomy ----------------------------------------------------------


class TestTaxonomy:
    def test_every_missing_row_gets_exactly_one_reason(self, db):
        for i in range(6):
            mk_market(db, f"X-{i}"); mk_forecast(db, f"X-{i}")
        mk_outcome(db, "X-1", status="closed", side=None)
        mk_outcome(db, "X-2", status="canceled", side="void")
        mk_outcome(db, "X-3", status="unknown", side=None)
        mk_outcome(db, "X-4", status="settled", side=None)

        rows = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured]
        missing = [r for r in rows if r.reason is not None]
        # X-0 and X-5 were never synced; X-1..X-4 each hold an unusable state
        # (settled-but-no-side included).
        assert len(missing) == 6
        assert all(r.reason in oc.ALL_REASONS for r in missing)
        report = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        assert sum(t["forecasts"] for t in report["taxonomy"]) == len(missing)

    def test_taxonomy_is_mutually_exclusive(self, db):
        """One reason per forecast, by construction — assert the classifier is
        a function, not a set of overlapping predicates."""
        for i in range(5):
            mk_market(db, f"M-{i}"); mk_forecast(db, f"M-{i}")
        mk_outcome(db, "M-1", status="closed", side=None)
        mk_outcome(db, "M-2", status="open", side=None)
        rows = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured]
        for r in rows:
            assert isinstance(r.reason, (str, type(None)))
        counted = sum(1 for r in rows if r.reason is not None)
        by_reason = {}
        for r in rows:
            if r.reason:
                by_reason.setdefault(r.reason, []).append(r.forecast_id)
        assert sum(len(v) for v in by_reason.values()) == counted
        seen = set()
        for ids in by_reason.values():
            assert not (seen & set(ids))
            seen |= set(ids)

    def test_closed_unsettled_stays_pending_not_a_loss(self, db):
        mk_market(db, "CU"); mk_forecast(db, "CU")
        mk_outcome(db, "CU", status="closed", side=None)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.MARKET_CLOSED_UNSETTLED
        assert row.recoverability == oc.REQUIRES_CURRENT_PROVIDER_SYNC
        assert not row.scored_current

    def test_canceled_market_is_unscorable(self, db):
        mk_market(db, "CA"); mk_forecast(db, "CA")
        mk_outcome(db, "CA", status="canceled", side=None)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.MARKET_CANCELED
        assert row.recoverability == oc.NOT_RECOVERABLE

    def test_void_market_is_unscorable(self, db):
        mk_market(db, "VO"); mk_forecast(db, "VO")
        mk_outcome(db, "VO", status="canceled", side="void")
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.MARKET_VOID
        assert row.recoverability == oc.NOT_RECOVERABLE

    def test_missing_winner_is_unscorable(self, db):
        mk_market(db, "MW"); mk_forecast(db, "MW")
        mk_outcome(db, "MW", status="settled", side=None)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.WINNER_MISSING

    def test_ambiguous_winner_is_preserved_not_normalized(self, db):
        mk_market(db, "AM"); mk_forecast(db, "AM")
        mk_outcome(db, "AM", status="settled", side="both")
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.WINNER_AMBIGUOUS
        assert row.outcome.winning_side == "both"  # preserved verbatim

    def test_unknown_status_is_not_normalized_into_yes_or_no(self, db):
        mk_market(db, "UN"); mk_forecast(db, "UN")
        mk_outcome(db, "UN", status="unknown", side=None)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.PROVIDER_STATUS_UNRECOGNIZED
        assert row.outcome.resolved_probability is None
        assert row.outcome.winning_side is None

    def test_stale_open_row_detected(self, db):
        mk_market(db, "ST", close_hours_ago=72)
        mk_forecast(db, "ST")
        mk_outcome(db, "ST", status="open", side=None, close_hours_ago=72)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.LOCAL_OUTCOME_STALE

    def test_conflicting_outcome_is_preserved_and_unscored(self, db):
        """side says yes, probability says no. Never repaired silently."""
        mk_market(db, "CF"); mk_forecast(db, "CF")
        mk_outcome(db, "CF", status="settled", side="yes", prob=0.0)
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.LOCAL_OUTCOME_CONFLICT
        assert row.outcome.winning_side == "yes"
        assert row.outcome.resolved_probability == 0.0

    def test_never_attempted_is_distinct_from_provider_missing(self, db):
        mk_market(db, "NA"); mk_forecast(db, "NA")
        row = [r for r in oc.load_coverage_rows(db, now=NOW) if r.matured][0]
        assert row.reason == oc.SYNC_NEVER_ATTEMPTED
        assert row.recoverability == oc.REQUIRES_CURRENT_PROVIDER_SYNC

    def test_no_reason_collapses_into_unknown(self):
        assert "unknown" not in oc.ALL_REASONS
        assert len(set(oc.ALL_REASONS)) == len(oc.ALL_REASONS)
        for reason in oc.ALL_REASONS:
            assert reason in oc._RECOVERABILITY, reason


# --- Gate 5/8: the selection defect and its repair -----------------------------


class TestSelectionRepair:
    def _bulk(self, db, n=30):
        for i in range(n):
            t = f"K-{i:03d}"
            mk_market(db, t)
            mk_forecast(db, t)
        return n

    def test_selection_audit_detects_the_frozen_prefix(self, db):
        self._bulk(db, 30)
        audit = oc.audit_selection(db, limit=10, repair_enabled=False)
        assert audit.verdict == "SELECTION_IS_A_FROZEN_ALPHABETICAL_PREFIX"
        assert audit.unreachable_tickers == 20
        assert audit.first_unreachable_rank == 10

    def test_selection_audit_clean_when_limit_covers_everything(self, db):
        self._bulk(db, 5)
        audit = oc.audit_selection(db, limit=50, repair_enabled=False)
        assert audit.verdict == "SELECTION_REACHES_EVERY_FORECASTED_MARKET"
        assert audit.unreachable_tickers == 0

    def test_repaired_selection_never_refetches_a_terminal_outcome(self, db):
        for t in ("A-1", "A-2", "A-3"):
            mk_market(db, t); mk_forecast(db, t)
        mk_outcome(db, "A-1", status="settled", side="yes")
        mk_outcome(db, "A-2", status="canceled", side="void")
        picked = OutcomeService(adapter=object()).select_sync_candidates(db, limit=10)
        assert "A-1" not in picked and "A-2" not in picked
        assert "A-3" in picked

    def test_repaired_selection_reaches_past_the_alphabetical_cap(self, db):
        """The defect: 'Z' ranked last was unreachable forever. Now the only
        thing that matters is whether its outcome can still move."""
        self._bulk(db, 30)
        mk_market(db, "Z-LAST"); mk_forecast(db, "Z-LAST")
        # Everything alphabetically before it is already terminal.
        for i in range(30):
            mk_outcome(db, f"K-{i:03d}", status="settled", side="no")
        picked = OutcomeService(adapter=object()).select_sync_candidates(db, limit=5)
        assert "Z-LAST" in picked

    def test_repaired_selection_prioritizes_oldest_close(self, db):
        mk_market(db, "OLD", close_hours_ago=500); mk_forecast(db, "OLD")
        mk_market(db, "MID", close_hours_ago=100); mk_forecast(db, "MID")
        mk_market(db, "NEW", close_hours_ago=3); mk_forecast(db, "NEW")
        picked = OutcomeService(adapter=object()).select_sync_candidates(db, limit=3)
        assert picked[0] == "OLD"
        assert picked.index("MID") < picked.index("NEW")

    def test_repaired_selection_respects_the_limit(self, db):
        self._bulk(db, 40)
        picked = OutcomeService(adapter=object()).select_sync_candidates(db, limit=7)
        assert len(picked) == 7
        assert len(set(picked)) == 7


class TestScoringSelectionRepair:
    def test_scoring_no_longer_stops_at_an_id_prefix(self, db):
        """The production symptom was exactly 1,000 scored forecasts spanning
        ids 1..1000 out of 12,543. Selection must follow need, not id order."""
        for i in range(20):
            t = f"S-{i:03d}"
            mk_market(db, t); mk_forecast(db, t)
            mk_outcome(db, t, status="settled", side="yes")
        svc = CalibrationService()
        # First pass scores only 5 (the cap).
        svc.score_unscored(db, limit=5)
        assert db.execute(select(ForecastScoreRecord)).scalars().all().__len__() == 5
        # Second pass must advance to NEW forecasts, not re-load the same 5.
        svc.score_unscored(db, limit=5)
        scored = {r.forecast_id for r in
                  db.execute(select(ForecastScoreRecord)).scalars().all()}
        assert len(scored) == 10

    def test_repeated_scoring_is_idempotent(self, db):
        mk_market(db, "ID"); mk_forecast(db, "ID")
        mk_outcome(db, "ID", status="settled", side="no")
        svc = CalibrationService()
        svc.score_unscored(db, limit=50)
        before = len(db.execute(select(ForecastScoreRecord)).scalars().all())
        for _ in range(3):
            svc.score_unscored(db, limit=50)
        assert len(db.execute(select(ForecastScoreRecord)).scalars().all()) == before

    def test_yes_no_flip_creates_a_new_score_and_keeps_history(self, db):
        mk_market(db, "FL"); f = mk_forecast(db, "FL", probability=0.75)
        row = mk_outcome(db, "FL", status="settled", side="yes")
        svc = CalibrationService()
        svc.score_unscored(db, limit=50)
        row.winning_side = "no"
        row.resolved_probability = 0.0
        db.commit()
        svc.score_unscored(db, limit=50)
        rows = db.execute(
            select(ForecastScoreRecord)
            .where(ForecastScoreRecord.forecast_id == f.id)
            .order_by(ForecastScoreRecord.id)).scalars().all()
        assert len(rows) == 2, "append-only audit trail must be preserved"
        assert rows[-1].brier_score == pytest.approx(0.75 ** 2)

    def test_no_score_for_an_unscorable_outcome(self, db):
        mk_market(db, "US"); mk_forecast(db, "US")
        mk_outcome(db, "US", status="canceled", side="void")
        CalibrationService().score_unscored(db, limit=50)
        row = db.execute(select(ForecastScoreRecord)).scalars().one()
        assert row.score_status == "unscorable"
        assert row.brier_score is None and row.log_loss is None
        assert row.was_resolved is False

    def test_forecast_probabilities_are_never_changed(self, db):
        mk_market(db, "PR"); f = mk_forecast(db, "PR", probability=0.42)
        mk_outcome(db, "PR", status="settled", side="yes")
        CalibrationService().score_unscored(db, limit=50)
        db.refresh(f)
        assert f.estimated_probability == 0.42


# --- Gate 6/7: recoverability and report purity --------------------------------


class TestCoverageReport:
    def test_recoverable_local_vs_provider_required(self, db):
        mk_market(db, "R1"); mk_forecast(db, "R1")                       # never attempted
        mk_market(db, "R2"); mk_forecast(db, "R2")
        mk_outcome(db, "R2", status="canceled", side="void")             # unrecoverable
        r = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        assert r["recoverability"][oc.REQUIRES_CURRENT_PROVIDER_SYNC] == 1
        assert r["recoverability"][oc.NOT_RECOVERABLE] == 1
        assert r["uplift"]["requires_new_provider"] == 0

    def test_uplift_is_labelled_an_upper_bound(self, db):
        mk_market(db, "U1"); mk_forecast(db, "U1")
        r = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        assert r["uplift"]["attainable_is_upper_bound"] is True
        assert r["uplift"]["max_attainable_coverage_pct"] >= \
            r["uplift"]["matured_coverage_now_pct"]

    def test_report_writes_nothing_and_calls_nothing(self, db):
        mk_market(db, "W1"); mk_forecast(db, "W1")
        before = {
            "outcomes": len(db.execute(select(MarketOutcomeRecord)).scalars().all()),
            "scores": len(db.execute(select(ForecastScoreRecord)).scalars().all()),
            "forecasts": len(db.execute(select(MarketForecastRecord)).scalars().all()),
        }
        r = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        after = {
            "outcomes": len(db.execute(select(MarketOutcomeRecord)).scalars().all()),
            "scores": len(db.execute(select(ForecastScoreRecord)).scalars().all()),
            "forecasts": len(db.execute(select(MarketForecastRecord)).scalars().all()),
        }
        assert before == after
        assert r["external_calls"] == 0 and r["persisted"] is False

    def test_text_json_parity(self, db, capsys):
        from app import cli

        mk_market(db, "P1"); mk_forecast(db, "P1")
        mk_outcome(db, "P1", status="settled", side="yes")
        assert cli.outcome_sync_coverage_report(fmt="json", session=db) == 0
        payload = json.loads(capsys.readouterr().out)
        assert cli.outcome_sync_coverage_report(fmt="text", session=db) == 0
        text = capsys.readouterr().out
        assert str(payload["funnel"]["matured_eligible"]) in text
        assert payload["verdict"] in text
        assert payload["funnel"]["settled_yes_no"] == 1

    def test_report_is_secret_free(self, db, capsys):
        from app import cli

        mk_market(db, "SF"); mk_forecast(db, "SF")
        cli.outcome_sync_coverage_report(fmt="json", session=db)
        out = capsys.readouterr().out.lower()
        for needle in ("api_key", "apikey", "secret", "password", "token=",
                       "authorization", "bearer"):
            assert needle not in out

    def test_invalid_window_is_refused(self, db, capsys):
        from app import cli

        assert cli.outcome_sync_coverage_report(hours=0, session=db) == 2
        assert cli.outcome_sync_coverage_report(
            since="2026-08-04T00:00:00Z", until="2026-08-01T00:00:00Z",
            session=db) == 2


# --- Gate 9: bounded backfill --------------------------------------------------


class _StubAdapter:
    def __init__(self, details):
        self.details = details
        self.calls: list[str] = []

    async def get_market_detail(self, ticker):
        self.calls.append(ticker)
        return self.details.get(ticker)


class TestBoundedBackfill:
    async def test_dry_run_is_pure(self, db):
        for i in range(3):
            mk_market(db, f"B-{i}"); mk_forecast(db, f"B-{i}")
        adapter = _StubAdapter({})
        result = await ob.run_backfill(
            db, confirm=False, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert result.markets_selected == 3
        assert result.provider_calls == 0
        assert adapter.calls == []
        assert result.persisted is False
        assert result.stop_reason == "dry_run"
        assert db.execute(select(MarketOutcomeRecord)).scalars().all() == []

    async def test_provider_cap_is_enforced(self, db):
        for i in range(10):
            mk_market(db, f"C-{i}"); mk_forecast(db, f"C-{i}")
        details = {f"C-{i}": {"ticker": f"C-{i}", "status": "finalized",
                              "result": "yes"} for i in range(10)}
        adapter = _StubAdapter(details)
        result = await ob.run_backfill(
            db, confirm=True, max_markets=4,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert result.provider_calls == 4
        assert len(adapter.calls) == 4
        assert result.stop_reason in ("provider_cap", "completed")
        assert len(db.execute(select(MarketOutcomeRecord)).scalars().all()) == 4

    async def test_absolute_cap_bounds_a_mistyped_flag(self, db):
        mk_market(db, "AC"); mk_forecast(db, "AC")
        result = await ob.run_backfill(
            db, confirm=False, max_markets=10 ** 9, now=NOW)
        assert result.provider_cap == ob.ABSOLUTE_MAX_MARKETS

    async def test_already_current_markets_are_excluded(self, db):
        mk_market(db, "D-1"); mk_forecast(db, "D-1")
        mk_outcome(db, "D-1", status="settled", side="yes")
        mk_market(db, "D-2"); mk_forecast(db, "D-2")
        adapter = _StubAdapter({"D-2": {"ticker": "D-2", "status": "finalized",
                                        "result": "no"}})
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert adapter.calls == ["D-2"]
        assert result.already_current_excluded == 1

    async def test_conflicts_are_excluded_not_overwritten(self, db):
        mk_market(db, "E-1"); mk_forecast(db, "E-1")
        mk_outcome(db, "E-1", status="settled", side="yes", prob=0.0)
        adapter = _StubAdapter({"E-1": {"ticker": "E-1", "status": "finalized",
                                        "result": "no"}})
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert adapter.calls == []
        assert result.conflicts_excluded == 1
        row = db.execute(select(MarketOutcomeRecord)).scalars().one()
        assert row.winning_side == "yes" and row.resolved_probability == 0.0

    async def test_provider_failure_is_isolated(self, db):
        for t in ("F-1", "F-2", "F-3"):
            mk_market(db, t); mk_forecast(db, t)
        adapter = _StubAdapter({"F-1": {"ticker": "F-1", "status": "finalized",
                                        "result": "yes"},
                                "F-3": {"ticker": "F-3", "status": "finalized",
                                        "result": "no"}})
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert result.provider_failures == 1  # F-2 returned None
        assert result.outcomes_created == 2
        assert result.stop_reason == "completed"

    async def test_unknown_status_is_persisted_as_unknown(self, db):
        mk_market(db, "G-1"); mk_forecast(db, "G-1")
        adapter = _StubAdapter({"G-1": {"ticker": "G-1", "status": "wat",
                                        "result": ""}})
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        row = db.execute(select(MarketOutcomeRecord)).scalars().one()
        assert row.outcome_status == "unknown"
        assert row.winning_side is None and row.resolved_probability is None
        assert result.unrecognized_status == 1

    async def test_closed_market_is_not_treated_as_resolved(self, db):
        mk_market(db, "H-1"); mk_forecast(db, "H-1")
        adapter = _StubAdapter({"H-1": {"ticker": "H-1", "status": "closed",
                                        "result": ""}})
        await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        row = db.execute(select(MarketOutcomeRecord)).scalars().one()
        assert row.outcome_status == "closed"
        assert row.resolved_probability is None
        CalibrationService().score_unscored(db, limit=10)
        score = db.execute(select(ForecastScoreRecord)).scalars().one()
        assert score.score_status == "pending_outcome"

    async def test_backfill_never_changes_a_forecast(self, db):
        mk_market(db, "I-1"); f = mk_forecast(db, "I-1", probability=0.33)
        adapter = _StubAdapter({"I-1": {"ticker": "I-1", "status": "finalized",
                                        "result": "yes"}})
        await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        db.refresh(f)
        assert f.estimated_probability == 0.33

    async def test_backfill_persists_outcomes_only_not_scores(self, db):
        """Gate 10: repository default is sync and scoring as separate stages."""
        mk_market(db, "J-1"); mk_forecast(db, "J-1")
        adapter = _StubAdapter({"J-1": {"ticker": "J-1", "status": "finalized",
                                        "result": "yes"}})
        await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert not hasattr(ob.BackfillResult, "scores_created"), (
            "a field that is never written is a metric that always reads 0")
        assert db.execute(select(ForecastScoreRecord)).scalars().all() == []
        # canonical scoring picks it up on the next cycle
        CalibrationService().score_unscored(db, limit=10)
        assert db.execute(select(ForecastScoreRecord)).scalars().one().score_status \
            == "scored"


# --- Gate 11: safety surface ---------------------------------------------------


class TestSafetySurface:
    FILES = (
        "app/services/outcome_coverage.py",
        "app/services/outcome_backfill.py",
    )

    def test_no_trading_or_ev_surface(self):
        """Structural, not textual.

        A substring scan over the whole file also matches the module's own
        safety DISCLAIMER, which names the very things it promises not to do —
        so the naive version fails on correct code and would train us to weaken
        it. Scan identifiers instead: names, attributes, functions, arguments.
        """
        banned = (
            "expected_value", "kelly", "position_size", "paper_trade",
            "place_order", "wallet", "private_key", "trade_recommendation",
            "execute_trade", "swap", "sign_transaction", "portfolio",
        )
        for rel in self.FILES:
            tree = ast.parse(Path(rel).read_text())
            identifiers = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    identifiers.add(node.id.lower())
                elif isinstance(node, ast.Attribute):
                    identifiers.add(node.attr.lower())
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                       ast.ClassDef)):
                    identifiers.add(node.name.lower())
                elif isinstance(node, ast.arg):
                    identifiers.add(node.arg.lower())
            for ident in identifiers:
                for needle in banned:
                    assert needle not in ident, f"{rel} defines {ident!r}"

    def test_no_outcome_is_inferred_from_price(self):
        """The one inference that would silently corrupt every label."""
        for rel in self.FILES:
            src = Path(rel).read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    assert node.attr not in (
                        "midpoint", "yes_bid", "yes_ask", "last_price",
                    ), f"{rel} reads a price attribute"

    def test_modules_never_construct_a_settled_outcome_themselves(self):
        """Only the shared adapter interpreter may decide yes/no."""
        tree = ast.parse(Path("app/services/outcome_backfill.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
        assert "parse_market_outcome" not in imported, (
            "backfill must reach settlement only through OutcomeService, so "
            "there is exactly one status interpreter in the codebase")
        # It must never construct an outcome record itself either.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "MarketOutcomeRecord"
            for kw in getattr(node, "keywords", []) or []:
                assert kw.arg not in ("resolved_probability", "winning_side"), (
                    "backfill must not assign a settlement field directly")

    def test_no_migration_added(self):
        versions = sorted(p.name for p in Path("alembic/versions").glob("0*.py"))
        assert versions[-1].startswith("0027"), (
            f"unexpected migration added: {versions[-1]}")

    def test_report_and_backfill_are_registered_in_the_cli(self):
        src = Path("app/cli.py").read_text()
        assert '"outcome-sync-coverage-report"' in src
        assert '"outcome-sync-backfill"' in src
        assert "confirm=args.confirm and not args.dry_run" in src


class TestFlagGating:
    """The flag has to be real, not decorative: OFF must reproduce the
    deployed defect exactly, or "dark deploy" is a story rather than a fact."""

    @pytest.fixture(autouse=True)
    def repair_disabled(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR", "false")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_flag_defaults_off(self, monkeypatch):
        from app.config import Settings

        monkeypatch.delenv("ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR", raising=False)
        assert Settings(
            database_url="sqlite:///x.db"
        ).enable_outcome_sync_coverage_repair is False

    async def test_off_keeps_the_alphabetical_prefix(self, db):
        for name in ("Z-1", "A-1", "M-1"):
            mk_market(db, name); mk_forecast(db, name)
        calls = []

        class _A:
            async def get_market_detail(self, ticker):
                calls.append(ticker)
                return {"ticker": ticker, "status": "active"}

        await OutcomeService(adapter=_A()).sync_known_markets(db, limit=2)
        assert calls == ["A-1", "M-1"], "OFF must be the deployed alphabetical order"

    def test_off_keeps_the_id_prefix_for_scoring(self, db):
        for i in range(6):
            t = f"P-{i}"
            mk_market(db, t); mk_forecast(db, t)
            mk_outcome(db, t, status="settled", side="yes")
        svc = CalibrationService()
        svc.score_unscored(db, limit=3)
        svc.score_unscored(db, limit=3)
        scored = {r.forecast_id for r in
                  db.execute(select(ForecastScoreRecord)).scalars().all()}
        # The deployed defect: the same 3 lowest ids, twice. It must survive
        # intact while the flag is off.
        assert scored == {1, 2, 3}


class TestStarvationGuard:
    """A failed fetch writes no row, so strict oldest-first would let a
    permanently-unfetchable head monopolise the budget forever — the same
    defect in a different sort key."""

    def test_rotation_reaches_every_candidate_despite_total_failure(self, db):
        for i in range(12):
            t = f"R-{i:03d}"
            mk_market(db, t, close_hours_ago=500 - i); mk_forecast(db, t)
        svc = OutcomeService(adapter=object())
        reached = set()
        for cycle in range(6):
            db.add(MarketOpsRun(status="ok", started_at=NOW))
            db.commit()
            reached |= set(svc.select_sync_candidates(db, limit=3))
        assert reached == {f"R-{i:03d}" for i in range(12)}, (
            "every candidate must be reached within ceil(n/limit) cycles even "
            "though not one fetch ever succeeded")

    def test_rotation_is_deterministic(self, db):
        for i in range(10):
            t = f"D-{i}"
            mk_market(db, t); mk_forecast(db, t)
        svc = OutcomeService(adapter=object())
        assert svc.select_sync_candidates(db, limit=3) == \
            svc.select_sync_candidates(db, limit=3)

    def test_no_rotation_when_everything_fits(self, db):
        for i in range(3):
            t = f"F-{i}"
            mk_market(db, t, close_hours_ago=100 - i); mk_forecast(db, t)
        svc = OutcomeService(adapter=object())
        for _ in range(4):
            db.add(MarketOpsRun(status="ok", started_at=NOW)); db.commit()
            picked = svc.select_sync_candidates(db, limit=10)
            assert picked[0] == "F-0", "priority must hold when the budget fits"


class TestBackfillExitSemantics:
    def test_dry_run_exits_zero(self, db, capsys):
        """The documented, default, SAFE invocation must not report failure —
        under `set -e` a non-zero dry run pushes the operator toward --confirm."""
        from app import cli

        mk_market(db, "EX"); mk_forecast(db, "EX")
        assert cli.outcome_sync_backfill(confirm=False, session=db) == 0
        assert "nothing written and nothing fetched" in capsys.readouterr().out

    async def test_all_fetches_failing_is_not_completed(self, db):
        for i in range(3):
            t = f"AF-{i}"
            mk_market(db, t); mk_forecast(db, t)
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=_StubAdapter({})), now=NOW)
        assert result.provider_calls == 3
        assert result.provider_failures == 3
        assert result.stop_reason == "all_fetches_failed"
        assert result.persisted is False


class TestAuditHonesty:
    """HIGH-2: the tool built to validate the repair must not report that the
    repair is absent once it ships."""

    def test_audit_reports_the_selection_that_is_actually_running(self, db):
        for i in range(30):
            t = f"H-{i:03d}"
            mk_market(db, t); mk_forecast(db, t)
        on = oc.audit_selection(db, limit=10, repair_enabled=True)
        off = oc.audit_selection(db, limit=10, repair_enabled=False)
        assert on.active_selection == "need_based"
        assert on.repair_enabled is True
        assert on.verdict == "SELECTION_IS_NEED_BASED_AND_ROTATES"
        assert on.unreachable_tickers == 0
        assert off.active_selection == "legacy_alphabetical_prefix"
        assert off.verdict == "SELECTION_IS_A_FROZEN_ALPHABETICAL_PREFIX"
        assert off.unreachable_tickers == 20

    def test_provider_market_missing_is_reachable(self, db):
        """MED-5: it was structurally dead, so requires_new_provider could only
        ever be zero — a constant presented as a measurement."""
        mk_market(db, "SEL"); mk_forecast(db, "SEL")
        row = [r for r in oc.load_coverage_rows(
            db, now=NOW, reachable_tickers={"SEL"}) if r.matured][0]
        assert row.reason == oc.PROVIDER_MARKET_MISSING
        assert row.recoverability == oc.REQUIRES_NEW_PROVIDER

        other = [r for r in oc.load_coverage_rows(
            db, now=NOW, reachable_tickers=set()) if r.matured][0]
        assert other.reason == oc.SYNC_NEVER_ATTEMPTED

    def test_unmeasurable_reasons_are_declared(self, db):
        mk_market(db, "UM"); mk_forecast(db, "UM")
        r = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        joined = " ".join(r["data_quality"])
        assert "NOT MEASURABLE" in joined
        for reason in oc.UNMEASURABLE_REASONS:
            assert reason in joined


class TestReviewFindingsSecondPass:
    """Claims the second independent review falsified with executable probes."""

    def test_a_self_contradicting_outcome_is_not_scored(self, db):
        """H1. `_score_target` decided on winning_side alone, so a row saying
        'yes' with resolved_probability 0.0 got a Brier value — while the
        coverage report called the same row a preserved, UNSCORED conflict.
        One of those two statements had to be false."""
        mk_market(db, "CFX"); mk_forecast(db, "CFX", probability=0.7)
        mk_outcome(db, "CFX", status="settled", side="yes", prob=0.0)
        CalibrationService().score_unscored(db, limit=10)
        row = db.execute(select(ForecastScoreRecord)).scalars().one()
        assert row.score_status == "unscorable"
        assert row.brier_score is None
        assert row.was_resolved is False

    def test_a_missing_probability_is_not_treated_as_a_conflict(self, db):
        """The narrow reading matters. `parse_market_outcome` always writes
        winning_side and resolved_probability together, so an absent
        probability is an older or synthetic row, not a contradiction —
        winning_side is still the source-backed field. Calling it a conflict
        would silently unscore a large, legitimate population."""
        mk_market(db, "CFY"); mk_forecast(db, "CFY", probability=0.7)
        row = mk_outcome(db, "CFY", status="settled", side="no")
        row.resolved_probability = None
        db.commit()
        CalibrationService().score_unscored(db, limit=10)
        scored = db.execute(select(ForecastScoreRecord)).scalars().one()
        assert scored.score_status == "scored"
        assert scored.brier_score == pytest.approx(0.49)
        assert [r for r in oc.load_coverage_rows(db, now=NOW)
                if r.matured][0].reason is None

    def test_funnel_is_monotonic_on_the_pair_that_broke(self, db):
        """H1's observable symptom: scored_current exceeded settled_yes_no."""
        for i in range(6):
            t = f"MN-{i}"
            mk_market(db, t); mk_forecast(db, t)
            mk_outcome(db, t, status="settled", side="yes")
        mk_market(db, "MN-BAD"); mk_forecast(db, "MN-BAD")
        mk_outcome(db, "MN-BAD", status="settled", side="yes", prob=0.0)
        CalibrationService().score_unscored(db, limit=50)
        f = oc.build_coverage_report(db, now=NOW, selection_limit=50).to_dict()["funnel"]
        assert f["settled_yes_no"] >= f["scored_current"]
        assert f["matured_eligible"] >= f["outcome_row_present"]

    def test_maturity_does_not_depend_on_outcome_presence_without_close_time(self, db):
        """H2. With market.close_time NULL, maturity used to fall back to the
        OUTCOME's close time — so having an outcome made a forecast matured.
        The bias was optimistic: it admitted mostly usable rows."""
        for name in ("NC-A", "NC-B"):
            m = mk_market(db, name)
            m.close_time = None
            mk_forecast(db, name)
        db.commit()
        mk_outcome(db, "NC-A", status="settled", side="yes")
        rows = {r.market_ticker: r for r in oc.load_coverage_rows(db, now=NOW)}
        assert rows["NC-A"].matured == rows["NC-B"].matured == False, (
            "maturity flipped purely because an outcome row existed")

    def test_audit_measures_the_sweep_instead_of_asserting_success(self, db):
        """H4. `unreachable = 0` was hard-coded when the repair was on, so the
        tool could never report that the repair was INSUFFICIENT."""
        for i in range(40):
            t = f"SW-{i:03d}"
            mk_market(db, t); mk_forecast(db, t)
        fast = oc.audit_selection(db, limit=40, repair_enabled=True)
        slow = oc.audit_selection(db, limit=1, repair_enabled=True)
        assert fast.candidate_pool == slow.candidate_pool == 40
        assert fast.full_sweep_cycles == 1
        assert slow.full_sweep_cycles == 40
        # 40 cycles x 360 s = 4 h — a real number derived from the pool and the
        # budget, not a constant that can only ever say "fine".
        assert slow.full_sweep_hours == 4.0
        assert fast.full_sweep_hours < slow.full_sweep_hours
        assert slow.verdict == "SELECTION_IS_NEED_BASED_AND_ROTATES"

    def test_audit_can_report_the_repair_is_insufficient(self, db, monkeypatch):
        for i in range(40):
            t = f"SL-{i:03d}"
            mk_market(db, t); mk_forecast(db, t)
        monkeypatch.setattr(oc, "MAX_HEALTHY_SWEEP_HOURS", 1.0)
        audit = oc.audit_selection(db, limit=1, repair_enabled=True)
        assert audit.verdict == "SELECTION_SWEEP_PERIOD_TOO_LONG"

    def test_rotation_survives_a_pruned_run_table(self, db):
        """M5. COUNT(*) stops being monotonic the day marketops_runs retention
        lands — already recommended in-repo — and the offset walks backwards."""
        for i in range(9):
            t = f"PR-{i}"
            mk_market(db, t); mk_forecast(db, t)
        svc = OutcomeService.__new__(OutcomeService)
        seen = set()
        for cycle in range(4):
            run = MarketOpsRun(status="ok", started_at=NOW)
            db.add(run); db.commit()
            seen |= set(svc.select_sync_candidates(db, limit=3))
            # simulate retention pruning everything older than the newest run
            for old in db.execute(
                select(MarketOpsRun).order_by(MarketOpsRun.id)
            ).scalars().all()[:-1]:
                db.delete(old)
            db.commit()
        assert len(seen) > 3, (
            "with a pruned run table the offset must still advance")

    def test_uplift_reports_both_a_loose_and_a_tight_bound(self, db):
        """M1. Markets that closed minutes ago and are merely awaiting
        settlement were counted as recoverable 'uplift'."""
        mk_market(db, "UB-1"); mk_forecast(db, "UB-1")
        mk_outcome(db, "UB-1", status="closed", side=None)
        mk_market(db, "UB-2"); mk_forecast(db, "UB-2")
        u = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()["uplift"]
        assert u["max_attainable_excluding_awaiting_settlement_pct"] < \
            u["max_attainable_coverage_pct"]

    def test_state_inconsistent_is_declared_unmeasurable(self):
        """M2. Maturity already requires a market row, so it cannot fire."""
        assert oc.STATE_INCONSISTENT in oc.UNMEASURABLE_REASONS


class TestFlagIsExactlyDark:
    """M3. With the flag OFF the brier-equality check must not fire, or merging
    the code re-scores flipped outcomes — a write the flag was meant to gate."""

    @pytest.fixture(autouse=True)
    def repair_disabled(self, monkeypatch):
        from app.config import get_settings

        monkeypatch.setenv("ENABLE_OUTCOME_SYNC_COVERAGE_REPAIR", "false")
        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_off_does_not_rescore_an_in_place_flip(self, db):
        mk_market(db, "DK"); f = mk_forecast(db, "DK", probability=0.7)
        row = mk_outcome(db, "DK", status="settled", side="yes")
        svc = CalibrationService()
        svc.score_unscored(db, limit=10)
        row.winning_side = "no"
        row.resolved_probability = 0.0
        db.commit()
        svc.score_unscored(db, limit=10)
        rows = db.execute(
            select(ForecastScoreRecord)
            .where(ForecastScoreRecord.forecast_id == f.id)).scalars().all()
        assert len(rows) == 1, "OFF must write nothing new — that is what dark means"


class TestActivationInstrumentation:
    """Defects the ACTIVATION surfaced in the coverage report itself. Neither
    affected the repair; both made the instrument misreport it."""

    def test_candidate_pool_excludes_non_forecasted_fallback(self, db):
        """Probing select_sync_candidates with a huge limit engages the
        non-forecasted fallback, so the 'pool' became the whole markets table:
        101,166 against 5,019 forecasted tickers on production, and a fictitious
        101-hour sweep. The production path (limit=100) never reaches it."""
        for i in range(6):
            t = f"CP-{i}"
            mk_market(db, t); mk_forecast(db, t)
        mk_outcome(db, "CP-0", status="settled", side="yes")   # terminal
        mk_outcome(db, "CP-1", status="canceled", side="void")  # terminal
        # markets nobody forecast — must never inflate the pool
        for i in range(50):
            mk_market(db, f"NOISE-{i:03d}")
        audit = oc.audit_selection(db, limit=2, repair_enabled=True)
        assert audit.distinct_forecasted_tickers == 6
        assert audit.candidate_pool == 4, (
            "pool must be non-terminal FORECASTED tickers, not the markets table")
        assert audit.full_sweep_cycles == 2

    def test_id_prefix_finding_is_suppressed_once_the_repair_is_on(self, db):
        """With the repair on, a contiguous scored prefix is the expected shape
        of a queue draining in id order — not a frozen prefix."""
        for i in range(5):
            t = f"DQ-{i}"
            mk_market(db, t); mk_forecast(db, t)
            mk_outcome(db, t, status="settled", side="yes")
        CalibrationService().score_unscored(db, limit=3)
        on = oc.build_coverage_report(db, now=NOW, selection_limit=10).to_dict()
        joined = " ".join(on["data_quality"])
        assert "id-ordered prefix, not a backlog" not in joined
        assert "draining in id order" in joined
