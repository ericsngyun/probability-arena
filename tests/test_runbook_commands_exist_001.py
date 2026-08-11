"""CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 B9 — every operator-facing
`python -m app.cli <command>` invocation named in the runbook must actually
exist as a registered subcommand. A prior round found archive-naming
commands documented but never written (dead operator instructions that fail
at the exact moment an operator is following them under pressure); this test
makes that class of drift a CI failure instead of something a human has to
notice by reading two files side by side.

Scope: `docs/EVO_X2_RUNBOOK.md`, the canonical operator-facing runbook this
milestone (B7/B9) rewrote. Extends to any other doc added to `DOCS_TO_SCAN`
below.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_TO_SCAN = [
    REPO_ROOT / "docs" / "EVO_X2_RUNBOOK.md",
]

# `python -m app.cli <subcommand>` (or `.venv/bin/python -m app.cli ...`) —
# deliberately anchored on the exact invocation form the runbook uses, not a
# loose "any backticked word" scan, to avoid false positives from prose.
COMMAND_PATTERN = re.compile(r"python -m app\.cli\s+([a-zA-Z][\w-]*)")


def _registered_commands() -> set[str]:
    parser = build_parser()
    # argparse exposes registered subparser names via the subparsers action.
    for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse has no public API for this
        if hasattr(action, "choices"):
            return set(action.choices.keys())
    raise AssertionError("could not find the subparsers action on build_parser()'s parser")


def _commands_named_in(doc: Path) -> set[str]:
    text = doc.read_text()
    return set(COMMAND_PATTERN.findall(text))


@pytest.mark.parametrize("doc", DOCS_TO_SCAN, ids=lambda d: d.name)
def test_every_documented_cli_command_is_registered(doc: Path):
    assert doc.exists(), f"scanned doc missing: {doc}"
    named = _commands_named_in(doc)
    assert named, f"regex found zero `python -m app.cli <cmd>` invocations in {doc} — pattern drift?"
    registered = _registered_commands()
    missing = sorted(named - registered)
    assert not missing, (
        f"{doc.name} names CLI command(s) that do not exist: {missing}. "
        "Either the doc has drifted or the command was renamed/removed."
    )


def test_registered_commands_sanity_check():
    """Regression pin: the extraction/registration mechanism itself must find
    a non-trivial number of commands, so a broken regex or a broken
    `build_parser()` import doesn't silently make the test above vacuous."""
    registered = _registered_commands()
    assert len(registered) > 50
    assert "db-schema-report" in registered
    assert "db-integrity-check" in registered
