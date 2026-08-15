from pathlib import Path

import pytest

from app import canon, cli

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DOCS = (
    "AGENTS.md",
    "docs/PROJECT_CANON.md",
    "docs/SAFETY_BOUNDARIES.md",
    "docs/CAPABILITY_MATRIX.md",
    "docs/ROADMAP.md",
    "docs/EVO_X2_RUNBOOK.md",
    "docs/FEATURE_FLAGS.md",
    "docs/TESTING_POLICY.md",
    "docs/ADR/ADR-001-read-only-first.md",
    "docs/ADR/ADR-002-no-wallet-private-keys.md",
    "docs/ADR/ADR-003-deterministic-hot-path.md",
    "docs/ADR/ADR-004-calibration-before-ev.md",
    "docs/ADR/ADR-005-baseball-canary-before-llm.md",
)


class TestDocsExist:
    @pytest.mark.parametrize("doc", REQUIRED_DOCS)
    def test_required_doc_exists_and_is_nonempty(self, doc):
        path = REPO_ROOT / doc
        assert path.is_file(), f"{doc} missing"
        assert len(path.read_text()) > 200, f"{doc} looks empty"

    def test_agents_md_covers_required_sections(self):
        text = (REPO_ROOT / "AGENTS.md").read_text().lower()
        for phrase in (
            "project purpose",
            "current phase",
            "agent roles",
            "required first steps",
            "allowed capabilities",
            "forbidden capabilities",
            "testing expectations",
            "deployment expectations",
            "report-back format",
            "agent-context",
        ):
            assert phrase in text, f"AGENTS.md missing section: {phrase}"

    def test_capability_matrix_mentions_forbidden_capabilities(self):
        text = (REPO_ROOT / "docs/CAPABILITY_MATRIX.md").read_text().lower()
        for capability in (
            "ev calculation",
            "paper trading",
            "live trading",
            "wallet execution",
            "crypto wallet",
            "autonomous execution",
            "crypto scouting",
        ):
            assert capability in text, f"capability matrix missing: {capability}"
        assert "❌" in (REPO_ROOT / "docs/CAPABILITY_MATRIX.md").read_text()

    def test_safety_boundaries_state_hard_limits(self):
        text = (REPO_ROOT / "docs/SAFETY_BOUNDARIES.md").read_text().lower()
        for phrase in (
            "ev calculation",
            "trade recommendations",
            "paper trading",
            "order placement",
            "wallet / private-key handling",
            "portfolio sizing",
            "autonomous trading",
        ):
            assert phrase in text, f"safety boundaries missing: {phrase}"

    def test_route_quote_amendment_states_its_narrow_permissions(self):
        """SAFETY-BOUNDARY-ROUTE-QUOTE-001 pins.

        The amendment permits two capability modes. What makes them safe is not
        the permission but the enumerated prohibitions and the hard artifact
        requirement — so those are what this pins. If any of these strings
        leaves the document, the boundary itself has changed and this test is
        supposed to fail.
        """
        raw = (REPO_ROOT / "docs/SAFETY_BOUNDARIES.md").read_text()
        for token in (
            "SAFETY-BOUNDARY-ROUTE-QUOTE-001",
            "READ_ONLY_ROUTE_QUOTE",
            "PAPER_SIMULATION",
        ):
            assert token in raw, f"route-quote amendment missing: {token}"

        text = raw.lower()
        for phrase in (
            # READ_ONLY_ROUTE_QUOTE: the inference that must stay closed.
            "swap instructions",
            "broadcasting",
            "wallet key material",
            # PAPER_SIMULATION: the two fields that keep a modeled number labeled.
            "model identifier",
            "modeled-vs-observed basis",
            # Permitting a quote never permits paying for one.
            "paid rpc",
            "solanatracker",
            # The policy/enforcement mismatch must stay documented, not discovered.
            "banned_identifier_fragments",
        ):
            assert phrase in text, f"route-quote amendment missing: {phrase}"

    def test_ast_audit_still_blocks_the_route_quote_identifiers(self):
        """Tripwire for the documented policy/enforcement mismatch.

        The amendment tells readers that the doc permits the capabilities while
        the AST audit still bans the identifiers. If someone unbans a fragment,
        that paragraph silently becomes false — so unbanning must land together
        with an update to the amendment, and this test is what forces the pair.
        """
        from app.services import frontier_eval

        for fragment in ("swap", "jupiter", "paper_trad", "portfolio", "position_siz"):
            assert fragment in frontier_eval.BANNED_IDENTIFIER_FRAGMENTS, (
                f"{fragment!r} left BANNED_IDENTIFIER_FRAGMENTS — that weakens an "
                "automated control and must be reflected in the "
                "SAFETY-BOUNDARY-ROUTE-QUOTE-001 interaction section"
            )
        assert "app/services/frontier_eval.py" in (
            REPO_ROOT / "docs/SAFETY_BOUNDARIES.md"
        ).read_text(), "the amendment must name the file that enforces the ban list"

    def test_canon_constants_align_with_boundaries(self):
        forbidden = " ".join(canon.FORBIDDEN_CAPABILITIES).lower()
        for term in ("ev", "paper trading", "order placement", "wallet", "autonomous"):
            assert term in forbidden

    def test_readme_points_agents_at_canon(self):
        text = (REPO_ROOT / "README.md").read_text()
        assert "AGENTS.md" in text
        assert "agent-context" in text


class TestAgentContextCli:
    async def test_agent_context_prints_canon(self, capsys):
        exit_code = await cli.agent_context()
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "project: probability-arena" in output
        assert "phase:" in output
        assert "database:" in output
        assert "feature flags:" in output
        assert "ENABLE_BASEBALL_EVIDENCE_FORECASTING" in output
        assert "allowed capabilities:" in output
        assert "forbidden capabilities" in output
        assert "wallet / private-key handling" in output
        assert "order placement" in output
        assert "expected services (EVO-X2):" in output
        assert "safe next milestones:" in output
        assert "AGENTS.md" in output

    async def test_agent_context_redacts_database_password(self, capsys, monkeypatch):
        from app.config import get_settings

        monkeypatch.setattr(
            get_settings(),
            "database_url",
            "postgresql+psycopg2://arena_user:supersecretpw@dbhost:5432/arena",
        )
        await cli.agent_context()
        output = capsys.readouterr().out
        assert "supersecretpw" not in output
        assert "arena_user:***@dbhost" in output

    def test_main_wires_agent_context(self, monkeypatch):
        captured = {}

        async def fake_context():
            captured["ran"] = True
            return 0

        monkeypatch.setattr(cli, "agent_context", fake_context)
        assert cli.main(["agent-context"]) == 0
        assert captured == {"ran": True}

class TestNarrowlyPermittedModes:
    """CANON-ROUTE-QUOTE-RECONCILE-001 pins.

    `NARROWLY_PERMITTED_MODES` is the one place canon describes something as
    permitted rather than forbidden, so it is the one place a future edit can
    widen the boundary without deleting a prohibition. The security review of
    the reconciliation found three ways that happens in practice, and each
    assertion below pins one of them.
    """

    def test_both_modes_carry_the_paid_source_exclusion(self):
        """The exclusion used to live only in a module COMMENT, and
        `agent-context` prints the tuple VALUES. An agent reading the
        documented first step therefore saw no paid-source prohibition, while
        the boundary doc forbids paid sources under BOTH modes. The constant
        also travels to any future importer without its comment."""
        for mode in canon.NARROWLY_PERMITTED_MODES:
            lowered = mode.lower()
            assert "solanatracker" in lowered, mode[:60]
            assert "paid rpc" in lowered, mode[:60]
            assert "free public endpoints only" in lowered, mode[:60]

    def test_both_modes_say_they_are_unimplemented_and_ungated(self):
        """"Permitted with conditions" reads as license to start building
        unless the string itself says otherwise."""
        for mode in canon.NARROWLY_PERMITTED_MODES:
            assert "NOT IMPLEMENTED" in mode, mode[:60]
            assert "STOP AND REPORT BACK" in mode, mode[:60]

    def test_permitted_modes_are_not_allowed_capabilities(self):
        """They are exceptions carved out of FORBIDDEN_CAPABILITIES, not
        surfaces this repo has. Promoting one into ALLOWED_CAPABILITIES would
        make it read as existing and implemented."""
        for mode in canon.NARROWLY_PERMITTED_MODES:
            name = mode.split(" ")[0]
            assert not any(name in a for a in canon.ALLOWED_CAPABILITIES), name

    def test_ev_prohibition_is_denomination_agnostic(self):
        """Narrowing this to "dollar EV" invites "EV in SOL / ticks /
        probability-weighted units is not dollar EV, so it is not on the
        list". docs/SAFETY_BOUNDARIES.md marks the EV row UNCHANGED by
        SAFETY-BOUNDARY-ROUTE-QUOTE-001, and docs/CAPABILITY_MATRIX.md still
        carries EV calculation as flatly non-existent."""
        ev = [c for c in canon.FORBIDDEN_CAPABILITIES if c.startswith("EV calculation")]
        assert len(ev) == 1, canon.FORBIDDEN_CAPABILITIES
        assert "ANY unit" in ev[0]
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        assert "any monetary or return-denominated expected value" in agents

    def test_agents_md_keeps_an_imperative_build_gate_for_both_modes(self):
        """The flat forbidden list is what the "stop and report back"
        imperative actually fires on. Describing a mode as permitted in a
        bullet, with its gate only as a trailing clause, removes the trigger."""
        agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        assert "BUILDING EITHER MODE TODAY STILL MEANS STOP AND REPORT BACK" in agents
        assert "MVP-005B" in agents
        assert "a separately accepted milestone that does not yet exist" in agents
        assert "paper trading beyond the modeled" in agents

    def test_agent_context_prints_the_modes_with_their_conditions(self, capsys):
        """Regression on the reason this tuple exists: agents read
        `agent-context` first, so the conditions must reach THAT OUTPUT — not
        merely exist in a constant. The original defect was precisely that the
        paid-source exclusion lived in a comment the command never prints."""
        import asyncio

        asyncio.run(cli.agent_context())
        output = capsys.readouterr().out
        assert "READ_ONLY_ROUTE_QUOTE" in output
        assert "PAPER_SIMULATION" in output
        assert "NOT IMPLEMENTED" in output
        assert "STOP AND REPORT BACK" in output
        assert "SolanaTracker" in output
        # the hard boundary must still be printed alongside the exceptions
        assert "forbidden capabilities" in output
