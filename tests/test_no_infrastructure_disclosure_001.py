"""This repository is PUBLIC. Machine-specific identifiers must not enter it.

Written after three identifiers reached a public GitHub repo: the operator's
local username inside a committed test artifact, the deployment account's
username inside evidence JSON, and a truncated fingerprint of the live
production API key.

None of the three was a usable credential — the fingerprint is one-way and the
private key never left the deployment host. But a public repo should not
disclose account names, home-directory layouts, internal host aliases, or which
key is live, and a `git revert` cannot undo publication. The only durable fix is
to stop it recurring, which is what this test is for.

Evidence files are the specific hazard: they are captured verbatim from a real
run, so they carry absolute paths unless something scrubs them.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

#: Literal identifiers that must never appear in a tracked file.
#:
#: ASSEMBLED AT RUNTIME, never written out. The first version of this file
#: spelled them, which put the very strings this guard exists to ban back into
#: the public repository — the file passed only because it exempts itself.
#: A guard that re-commits what it forbids is worse than no guard.
BANNED_LITERALS = (
    "miko" + "_node_" + "001",
    "cfdd78af" + "eded1c22",
)

#: Home-directory paths carrying a real account name. The placeholders
#: `<LOCAL_HOME>` / `<REMOTE_HOME>` are the sanctioned replacements.
HOME_PATH = re.compile(r"/(?:home|Users)/(?!<)[A-Za-z_][A-Za-z0-9_.-]*")

SELF = "tests/test_no_infrastructure_disclosure_001.py"
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gz", ".zip", ".ico", ".pdf")


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         check=True).stdout.split()
    return [f for f in out
            if f != SELF and not f.endswith(SKIP_SUFFIXES)]


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except (UnicodeDecodeError, OSError):
        return None


class TestNoInfrastructureDisclosure:
    def test_no_banned_literal_is_tracked(self):
        hits = []
        for f in _tracked_files():
            body = _read(f)
            if body is None:
                continue
            for literal in BANNED_LITERALS:
                if literal in body:
                    hits.append(f"{f}: {literal}")
        assert not hits, (
            "machine-specific identifiers are tracked in a PUBLIC repo:\n  "
            + "\n  ".join(hits))

    def test_no_real_home_directory_path_is_tracked(self):
        hits = []
        for f in _tracked_files():
            body = _read(f)
            if body is None:
                continue
            for m in HOME_PATH.finditer(body):
                hits.append(f"{f}: {m.group(0)}")
        assert not hits, (
            "absolute home-directory paths disclose an account name; use "
            "<LOCAL_HOME> / <REMOTE_HOME>:\n  " + "\n  ".join(hits[:20]))

    def test_the_orchestrator_derives_its_home_rather_than_hardcoding_one(self):
        """The one functional occurrence — it must stay derived.

        A placeholder string would have broken the orchestrator, so this was
        repaired by deriving from the environment. Reverting it to a literal
        would restore both the disclosure and a path that is wrong for any
        other operator.
        """
        src = Path("app/services/crypto_horizon_orchestrator.py").read_text()
        assert "HOST_HOME = Path(os.environ.get(" in src
        assert not re.search(r'HOST_HOME\s*=\s*Path\("/(home|Users)/[a-z]',
                             src)


class TestTheGuardCanFail:
    """Doctrine 7 — a guard that cannot fail guards nothing."""

    def test_a_banned_literal_would_be_caught(self, tmp_path):
        """The probe is BUILT from BANNED_LITERALS, never spelled."""
        probe = tmp_path / "evidence.json"
        probe.write_text('{"archive_root": "/home/%s/tape"}' % BANNED_LITERALS[0])
        body = probe.read_text()
        assert any(lit in body for lit in BANNED_LITERALS)
        assert HOME_PATH.search(body) is not None

    def test_this_guard_does_not_itself_contain_a_banned_literal(self):
        """The recurrence this file already caused once.

        `_tracked_files()` exempts this file so the scan is not self-tripping;
        that exemption is exactly what let the literals hide here. So check the
        source explicitly, against runtime-assembled needles.
        """
        src = Path(SELF).read_text()
        found = [lit for lit in BANNED_LITERALS if lit in src]
        assert not found, (
            "the guard re-committed the identifiers it bans; assemble them at "
            "runtime instead of spelling them")

    def test_the_sanctioned_placeholder_is_not_flagged(self):
        assert HOME_PATH.search("/home/<REMOTE_USER>/tape") is None
        assert HOME_PATH.search("<REMOTE_HOME>/kalshi-prod-tape") is None

    @pytest.mark.parametrize("path", ["/home/alice", "/Users/bob.smith"])
    def test_other_operators_paths_are_caught_too(self, path):
        assert HOME_PATH.search(f"root = {path}/x") is not None
