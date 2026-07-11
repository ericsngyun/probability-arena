"""EDGE-RETIRE-001 tests: retirement registry + report.

Registry contents match the retirement record, retired candidates are flagged
in the validation report (status machinery untouched), the retirement CLI
renders frozen record + live post-lock section + resurrection rule, the
retirement document carries the required sections, no persistence, no
forbidden vocabulary.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import cli
from app.db import Base
from app.services.edge_selection import (
    PREREGISTERED,
    RETIRED_CANDIDATES,
    RETIREMENT_DOC,
    ROLE_CANDIDATE,
    EdgeSelectionValidationReportService,
)

NOW = datetime.now(timezone.utc)
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestRegistry:
    def test_all_six_candidates_retired(self):
        candidates = {n for n, role, _ in PREREGISTERED if role == ROLE_CANDIDATE}
        assert set(RETIRED_CANDIDATES) == candidates
        assert len(RETIRED_CANDIDATES) == 6

    def test_frozen_numbers_present(self):
        rec = RETIRED_CANDIDATES["require_gap_follows_move_totals_only"]
        assert rec["discovery"] == "0.539/+0.42"
        assert rec["validation"] == "0.286/-1.22"

    def test_baseline_and_control_not_retired(self):
        assert "baseline_all_watchlist" not in RETIRED_CANDIDATES
        assert "spread_only" not in RETIRED_CANDIDATES


class TestValidationReportFlags:
    def test_retired_flag_and_note_on_candidates(self, session):
        r = EdgeSelectionValidationReportService().build(
            session, since=NOW - timedelta(hours=1), lock=NOW - timedelta(hours=2)
        )
        for p in r["policies"]:
            if p["name"] in RETIRED_CANDIDATES:
                assert p.get("retired") is True
                assert "RETIRED" in p["status_reason"]
            else:
                assert "retired" not in p
        # protocol statuses themselves are untouched by retirement
        assert r["mvp_005b_note"].startswith("MVP-005B remains blocked")


class TestCLI:
    def test_retirement_report_renders(self, session, capsys):
        n = asyncio.run(cli.edge_selection_retirement_report(session=session))
        out = capsys.readouterr().out
        assert n == 6
        assert "never advice" in out
        assert "0.286/-1.22" in out                       # frozen record
        assert "OVERFIT" in out
        assert "NEW prereg + NEW lock" in out
        assert "MVP-005B remains blocked" in out
        assert "[RETIRED]" in out                          # live section flags

    def test_cli_parses(self, monkeypatch):
        called = {}

        async def fake(**kw):
            called["yes"] = True
            return 6

        monkeypatch.setattr(cli, "edge_selection_retirement_report", fake)
        assert cli.main(["edge-selection-retirement-report"]) == 0
        assert called


class TestDocument:
    def test_retirement_doc_sections(self):
        doc = (REPO / RETIREMENT_DOC).read_text()
        for required in (
            "RETIRED (all six candidates)",
            "The verdict",
            "Cost-adjusted confirmation",
            "negative-control inversion",
            "The policy search overfit",
            "MVP-005B remains blocked",
            "NEW pre-registration document with a NEW lock",
            "mechanism-first",
        ):
            assert required in doc, required


class TestSafety:
    def test_no_forbidden_vocab(self):
        import io
        import tokenize

        src = (REPO / "app" / "services" / "edge_selection.py").read_text()
        toks = [t.string.lower() for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.STRING, tokenize.COMMENT)]
        code = " ".join(toks)
        for bad in ("expected_value", "kelly", "position_siz", "paper_trad",
                    "place_order", "wallet", "private_key", "arbitrage",
                    "pnl", "profit", "swap", "execution", "markov"):
            assert bad not in code, bad
