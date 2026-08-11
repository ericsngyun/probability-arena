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

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from app.cli import build_parser
from app.db import Base

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_TO_SCAN = [
    REPO_ROOT / "docs" / "EVO_X2_RUNBOOK.md",
]

# `python -m app.cli <subcommand>` (or `.venv/bin/python -m app.cli ...`) —
# deliberately anchored on the exact invocation form the runbook uses, not a
# loose "any backticked word" scan, to avoid false positives from prose.
COMMAND_PATTERN = re.compile(r"python -m app\.cli\s+([a-zA-Z][\w-]*)")

# CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R5 — the scan above only
# ever covered `python -m app.cli` invocations, so the runbook's INLINE
# `python -c "..."` snippets were completely untested. One of them was
# broken: `SELECT count(*) FROM sqlite_stat1` against an un-ANALYZEd
# database raises `OperationalError: no such table: sqlite_stat1`, because
# SQLite creates that table only when `ANALYZE` first runs — and an
# un-ANALYZEd database is exactly, and only, what the restore-verification
# procedure exists to detect (every backup artifact on the host predates the
# live ANALYZE). The `if 0` branch was therefore unreachable for the one
# input that matters. These snippets are executed for real below.
INLINE_PY_PATTERN = re.compile(
    r'python -c "\n(.*?)\n[ \t]*"[ \t]*$', re.DOTALL | re.MULTILINE
)


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


def _inline_python_snippets(doc: Path) -> list[str]:
    """The runbook's snippets are embedded in a double-quoted shell string,
    so `\\"` in the markdown is a literal `"` by the time python sees it.
    Snippets inside numbered lists are indented; dedent them."""
    out = []
    for body in INLINE_PY_PATTERN.findall(doc.read_text()):
        out.append(textwrap.dedent(body).replace('\\"', '"'))
    return out


def _fresh_unanalyzed_db(path: Path) -> None:
    """A real, schema-complete database that has NEVER been ANALYZEd — i.e.
    with no `sqlite_stat1` table at all. This is the state of every backup
    artifact the restore procedure is written to inspect."""
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    con = __import__("sqlite3").connect(path)
    try:
        present = con.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='sqlite_stat1'"
        ).fetchone()[0]
    finally:
        con.close()
    assert present == 0, "fixture is not un-ANALYZEd; the test would be vacuous"


@pytest.mark.parametrize("doc", DOCS_TO_SCAN, ids=lambda d: d.name)
def test_runbook_has_inline_python_snippets_to_check(doc: Path):
    """Pin the extraction mechanism so a regex drift cannot silently make the
    execution test below vacuous."""
    snippets = _inline_python_snippets(doc)
    assert len(snippets) >= 4, (
        f"expected at least 4 inline `python -c` snippets in {doc.name}, "
        f"found {len(snippets)} — pattern drift?"
    )


@pytest.mark.parametrize("index", range(4))
def test_every_inline_python_snippet_runs_against_an_unanalyzed_database(
    tmp_path, index
):
    """R5 — each runbook `python -c` snippet must actually RUN against the
    kind of database it will meet in production. Reverting the existence
    gate in the restore snippet reproduces the reported crash
    (`OperationalError: no such table: sqlite_stat1`) and fails here.

    Each snippet gets its own fresh un-ANALYZEd database, because two of
    them run `ANALYZE` and would otherwise create `sqlite_stat1` for the
    snippets that follow — hiding the exact defect this test exists to
    catch."""
    doc = REPO_ROOT / "docs" / "EVO_X2_RUNBOOK.md"
    snippets = _inline_python_snippets(doc)
    assert index < len(snippets), f"snippet {index} missing from {doc.name}"
    snippet = snippets[index]

    db = tmp_path / f"snippet_{index}.db"
    _fresh_unanalyzed_db(db)

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db}"
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"runbook snippet #{index} failed against an un-ANALYZEd database.\n"
        f"--- snippet ---\n{snippet}\n--- stderr ---\n{proc.stderr[-2000:]}"
    )


def test_no_doc_claims_an_integrity_check_duration_the_evidence_refutes():
    """CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001 R0 — two reviewers
    reported `PRAGMA integrity_check` durations on the production-scale
    database that differ by two orders of magnitude (6.59s vs 669.34s), and
    the runbook carried a third figure, `~7m18s` (438s), that the milestone
    doc admits was never measured.

    The repository holds the arbiter on disk: `analyze_live.json` is the
    record of a real session against the live 4 550 623 232-byte EVO-X2
    database, and that session INCLUDED a full `PRAGMA integrity_check`
    returning `ok`. Its total wall time therefore places a hard UPPER BOUND
    on how long the check can possibly take on that host. Any doc claiming
    a duration longer than the whole session is refuted by the repo's own
    evidence.

    Reverting the duration correction (restoring `~7m18s`) fails this."""
    from datetime import datetime

    artifact = (
        REPO_ROOT / "docs" / "evidence"
        / "crypto-query-plan-and-denominator-recovery-001" / "analyze_live.json"
    )
    assert artifact.exists(), f"evidence artifact missing: {artifact}"
    data = json.loads(artifact.read_text())
    assert data.get("integrity_check") == "ok", (
        "artifact no longer records a full integrity_check; this test's "
        "upper bound is only valid because that check was inside the session"
    )
    session_seconds = (
        datetime.fromisoformat(data["finished_at"])
        - datetime.fromisoformat(data["started_at"])
    ).total_seconds()
    assert 0 < session_seconds < 60, (
        f"unexpected session length {session_seconds}s — re-derive the bound"
    )

    # Any "<N>m<M>s" duration asserted next to an integrity-check mention.
    minutes_pattern = re.compile(r"~?(\d+)m(\d+)s")
    offenders = []
    for doc in [
        REPO_ROOT / "docs" / "EVO_X2_RUNBOOK.md",
        REPO_ROOT / "docs" / "milestones"
        / "CRYPTO-BACKLOG-SELECTION-AND-OPERATOR-PATH-001.md",
        REPO_ROOT / "app" / "cli.py",
    ]:
        for line in doc.read_text().splitlines():
            if "integrity" not in line.lower() and "integrity-check" not in line:
                continue
            for mins, secs in minutes_pattern.findall(line):
                claimed = int(mins) * 60 + int(secs)
                # A line may quote the WRONG figure while correcting it; only
                # flag lines that are not doing exactly that.
                if any(w in line.lower() for w in
                       ("wrong", "never measured", "corrected", "transcription",
                        "not 7m18s", "used to claim", "refute", "cannot fit",
                        "once documented")):
                    continue
                if claimed > session_seconds:
                    offenders.append((doc.name, claimed, line.strip()[:110]))
    assert not offenders, (
        f"doc(s) claim an integrity_check duration longer than the entire "
        f"{session_seconds:.3f}s live session recorded in analyze_live.json "
        f"(which itself contained a full integrity_check): {offenders}"
    )


def test_registered_commands_sanity_check():
    """Regression pin: the extraction/registration mechanism itself must find
    a non-trivial number of commands, so a broken regex or a broken
    `build_parser()` import doesn't silently make the test above vacuous."""
    registered = _registered_commands()
    assert len(registered) > 50
    assert "db-schema-report" in registered
    assert "db-integrity-check" in registered
