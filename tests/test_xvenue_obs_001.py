"""XVENUE-OBS-001 tests: read-only cross-venue observation-window report.

The report composes ALREADY-PERSISTED rows (latest Polymarket scan run + latest
cross-venue match run) into one window verdict: clean vs flagged comparables,
side-uncertain counts, mismatch reasons, and an overlap assessment. Coverage
intelligence only — never arbitrage/EV/trade language. No live network;
in-memory SQLite; rows are seeded directly (the matcher itself is tested in
test_poly_002 / test_poly_precision_001 / test_xvenue_ops_001).
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
    CrossVenueMarketCandidate,
    CrossVenueObservationRun,
    PolymarketScoutRun,
)
from app.services.xvenue_observation import (
    ASSESS_CLEAN_COMPARABLE,
    ASSESS_INSUFFICIENT,
    ASSESS_NO_CLEAN_COMPARABLE,
    ASSESS_NO_MATCH_RUN,
    ASSESS_NO_SCAN,
    MIN_CANDIDATES_FOR_OVERLAP,
    XVenueObservationReport,
    XVenueObservationReportService,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def scan(session, *, started=None, mode="targeted", queries=("world cup",), markets=400):
    row = PolymarketScoutRun(
        status="ok", started_at=started or NOW, finished_at=(started or NOW),
        markets_seen=markets, markets_persisted=markets, scan_mode=mode,
        queries_used=list(queries), created_at=started or NOW,
    )
    session.add(row)
    session.flush()
    return row


def match_run(session, *, started=None, kalshi=4000, poly=447):
    row = CrossVenueObservationRun(
        status="ok", started_at=started or NOW, finished_at=(started or NOW),
        kalshi_markets_considered=kalshi, polymarket_markets_considered=poly,
        created_at=started or NOW,
    )
    session.add(row)
    session.flush()
    return row


def candidate(session, run, *, label="unresolved_semantic_match", diff=None,
              reasons=(), conf=0.5, domain="sports", ticker="KXWCGAME-1", pm="PM1"):
    c = CrossVenueMarketCandidate(
        run_id=run.id, kalshi_ticker=ticker, polymarket_market_id=pm, domain=domain,
        match_label=label, match_confidence=conf, observed_difference=diff,
        midpoint_difference=diff, mismatch_reasons=list(reasons),
        match_reasons=["title_similarity=0.5"], created_at=NOW,
    )
    session.add(c)
    session.flush()
    run.candidates_created = (run.candidates_created or 0) + 1
    return c


def build(session, **kw) -> XVenueObservationReport:
    session.commit()
    return XVenueObservationReportService().build(session, **kw)


# --- overlap assessments -------------------------------------------------------


class TestAssessments:
    def test_empty_db_is_no_scan_data(self, session):
        r = build(session)
        assert r.overlap_assessment == ASSESS_NO_SCAN
        assert "polymarket-scan-once" in r.assessment_detail

    def test_scan_without_match_is_no_match_run(self, session):
        scan(session)
        r = build(session)
        assert r.overlap_assessment == ASSESS_NO_MATCH_RUN
        assert "cross-venue-match-once" in r.assessment_detail

    def test_few_candidates_is_insufficient_overlap(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run)
        r = build(session)
        assert r.overlap_assessment == ASSESS_INSUFFICIENT
        assert r.candidates == 1

    def test_candidates_without_clean_comparable(self, session):
        scan(session)
        run = match_run(session)
        for i in range(MIN_CANDIDATES_FOR_OVERLAP):
            candidate(session, run, label="incompatible_outcome",
                      reasons=["outcome_type_mismatch=a!=b"], pm=f"PM{i}")
        candidate(session, run, label="comparable_market_candidate", diff=0.51,
                  reasons=["large_observed_difference_requires_review=0.51"], pm="PMF")
        r = build(session)
        assert r.overlap_assessment == ASSESS_NO_CLEAN_COMPARABLE
        assert r.comparable_total == 1
        assert r.comparable_clean == 0
        assert r.comparable_flagged == 1

    def test_clean_comparable_present(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="comparable_market_candidate", diff=0.02, pm="PMC")
        r = build(session)
        assert r.overlap_assessment == ASSESS_CLEAN_COMPARABLE
        assert r.comparable_clean == 1
        assert r.clean_candidates[0]["polymarket_market_id"] == "PMC"

    def test_match_before_scan_is_noted(self, session):
        run = match_run(session, started=NOW - timedelta(hours=2))
        candidate(session, run, label="comparable_market_candidate", diff=0.02)
        scan(session, started=NOW)  # scan came AFTER the match
        r = build(session)
        assert r.match_ran_after_scan is False
        assert "rerun" in r.assessment_detail.lower()

    def test_match_after_scan_not_noted(self, session):
        scan(session, started=NOW - timedelta(hours=1))
        run = match_run(session, started=NOW)
        candidate(session, run, label="comparable_market_candidate", diff=0.02)
        r = build(session)
        assert r.match_ran_after_scan is True
        assert "rerun" not in r.assessment_detail.lower()


# --- composition ---------------------------------------------------------------


class TestComposition:
    def test_scan_provenance_surfaces(self, session):
        scan(session, mode="catalog+targeted", queries=("mlb", "world cup"), markets=396)
        match_run(session)
        r = build(session)
        assert r.scan_mode == "catalog+targeted"
        assert r.scan_queries == ["mlb", "world cup"]
        assert r.scan_markets_seen == 396

    def test_clean_and_flagged_are_separated(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="comparable_market_candidate", diff=0.02,
                  conf=0.9, pm="CLEAN")
        candidate(session, run, label="comparable_market_candidate", diff=0.51,
                  reasons=["large_observed_difference_requires_review=0.51"], pm="FLAG")
        r = build(session)
        assert [c["polymarket_market_id"] for c in r.clean_candidates] == ["CLEAN"]
        assert [c["polymarket_market_id"] for c in r.flagged_candidates] == ["FLAG"]

    def test_side_uncertain_counted(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, reasons=["outcome_side_uncertain"], pm="A")
        candidate(session, run, reasons=["midpoint_side_uncertain"], pm="B")
        candidate(session, run, reasons=["resolution_gap_days=20"], pm="C")
        r = build(session)
        assert r.side_uncertain == 2

    def test_mismatch_reasons_aggregated_by_key(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, reasons=["outcome_type_mismatch=a!=b"], pm="A")
        candidate(session, run, reasons=["outcome_type_mismatch=c!=d",
                                         "entity_mismatch"], pm="B")
        r = build(session)
        assert r.mismatch_reasons["outcome_type_mismatch"] == 2
        assert r.mismatch_reasons["entity_mismatch"] == 1

    def test_by_label_and_domain(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="incompatible_outcome", domain="sports", pm="A")
        candidate(session, run, label="unresolved_semantic_match", domain="politics", pm="B")
        r = build(session)
        assert r.by_label == {"incompatible_outcome": 1, "unresolved_semantic_match": 1}
        assert r.by_domain == {"sports": 1, "politics": 1}
        assert r.unresolved == 1

    def test_only_latest_match_run_counts(self, session):
        scan(session)
        old = match_run(session, started=NOW - timedelta(hours=3))
        candidate(session, old, label="comparable_market_candidate", diff=0.01, pm="OLD")
        new = match_run(session, started=NOW)
        candidate(session, new, label="incompatible_outcome", pm="NEW")
        r = build(session)
        assert r.match_run_id == new.id
        assert r.comparable_total == 0  # OLD's comparable not counted

    def test_top_bounds_candidate_lists(self, session):
        scan(session)
        run = match_run(session)
        for i in range(6):
            candidate(session, run, label="comparable_market_candidate",
                      diff=0.01, conf=0.5 + i / 100, pm=f"PM{i}")
        r = build(session, top=3)
        assert len(r.clean_candidates) == 3
        # highest-confidence first
        assert r.clean_candidates[0]["polymarket_market_id"] == "PM5"


# --- CLI -----------------------------------------------------------------------


class TestCLI:
    def test_cli_runs_and_prints_verdict(self, session, capsys):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="comparable_market_candidate", diff=0.02)
        session.commit()

        n = asyncio.run(cli.xvenue_observation_report(session=session))
        out = capsys.readouterr().out
        assert n == 1
        assert "overlap assessment: clean_comparable_present" in out
        assert "clean comparable candidates" in out
        assert "not advice" in out

    def test_cli_zero_candidates_is_success_not_error(self, session, capsys):
        session.commit()
        n = asyncio.run(cli.xvenue_observation_report(session=session))
        assert n == 0  # a valid empty result, exit 0
        assert "no_scan_data" in capsys.readouterr().out

    def test_cli_parses_top(self, monkeypatch):
        captured = {}

        async def fake(**kw):
            captured.update(kw)
            return 0

        monkeypatch.setattr(cli, "xvenue_observation_report", fake)
        rc = cli.main(["xvenue-observation-report", "--top", "5"])
        assert rc == 0
        assert captured["top"] == 5

    def test_flagged_rows_printed_with_review_language(self, session, capsys):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="comparable_market_candidate", diff=0.51,
                  reasons=["large_observed_difference_requires_review=0.51"])
        session.commit()

        asyncio.run(cli.xvenue_observation_report(session=session))
        out = capsys.readouterr().out
        assert "FLAGGED for review" in out
        assert "not opportunities" in out


# --- safety --------------------------------------------------------------------


class TestSafety:
    def test_report_has_no_forbidden_fields(self):
        fields = set(XVenueObservationReport.__annotations__)
        for bad in ("ev", "expected_value", "side", "size", "profit", "edge",
                    "arbitrage", "arb", "opportunity", "order", "wallet",
                    "recommendation", "action"):
            assert bad not in fields

    def test_assessments_use_coverage_language(self):
        from app.services import xvenue_observation as mod

        values = " ".join(
            str(getattr(mod, n)) for n in dir(mod) if n.startswith("ASSESS_")
        ).lower()
        for bad in ("arbitrage", "opportunity", "edge", "profit", "trade", "buy", "sell"):
            assert bad not in values

    def test_no_forbidden_vocab_in_executable_code(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "xvenue_observation.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("arbitrage", "opportunity", "expected_value", "paper_trad",
                    "place_order", "wallet", "private_key", "kelly", "position_siz",
                    "swap", "jupiter"):
            assert bad not in code

    def test_module_makes_no_external_calls(self):
        src = (REPO / "app" / "services" / "xvenue_observation.py").read_text()
        for net in ("httpx", "requests", "urllib", "aiohttp", "socket"):
            assert net not in src

    def test_report_persists_nothing(self, session):
        scan(session)
        run = match_run(session)
        candidate(session, run, label="comparable_market_candidate", diff=0.02)
        session.commit()
        before = {
            t: session.execute(__import__("sqlalchemy").text(f"select count(*) from {t}")).scalar()
            for t in ("polymarket_scout_runs", "cross_venue_observation_runs",
                      "cross_venue_market_candidates")
        }
        XVenueObservationReportService().build(session)
        session.commit()
        after = {
            t: session.execute(__import__("sqlalchemy").text(f"select count(*) from {t}")).scalar()
            for t in before
        }
        assert before == after
