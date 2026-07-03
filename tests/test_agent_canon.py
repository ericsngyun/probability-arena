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