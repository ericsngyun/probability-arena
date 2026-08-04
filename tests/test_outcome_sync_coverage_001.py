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
        audit = oc.audit_selection(db, limit=10)
        assert audit.verdict == "SELECTION_IS_A_FROZEN_ALPHABETICAL_PREFIX"
        assert audit.unreachable_tickers == 20
        assert audit.first_unreachable_rank == 10

    def test_selection_audit_clean_when_limit_covers_everything(self, db):
        self._bulk(db, 5)
        audit = oc.audit_selection(db, limit=50)
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
        result = await ob.run_backfill(
            db, confirm=True, max_markets=10,
            service=OutcomeService(adapter=adapter), now=NOW)
        assert result.scores_created == 0
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
