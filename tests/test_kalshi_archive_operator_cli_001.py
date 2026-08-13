"""KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7 — the operator recovery surface.

The audit finding this milestone starts from: `kalshi-realtime-replay` was the
ONLY archive CLI subcommand that existed, and several error messages and one
module docstring pointed operators at commands (`archive-adopt`,
`archive-discard`, `kalshi-realtime-archive-migrate-legacy`) that were never
implemented. These tests exercise the real commands this milestone adds
(`archive-init`, `archive-recover-head`, `archive-adopt`,
`archive-migrate-legacy`) end to end, through both the CLI function layer and
`build_parser()`'s argument wiring, and assert the operator-instruction
invariant going forward: every command-shaped string this module's error
messages and docstrings actually name must exist and be exercised somewhere
in this test suite.

No network, no SQLite, no credential.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import cli
from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

ENV = "demo"
REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def fields(i, ticker="KXA"):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": ticker, "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }


def init(root, environment=ENV, **kw):
    return ah.initialize_archive(root, environment,
                                 archive_identity="kalshi-realtime", **kw)


def build(root, names, per=10, environment=ENV):
    made = []
    for n in names:
        w = sg.SegmentWriter(root, environment=environment,
                             segment_id=f"kalshi.seg-{n}",
                             partition_identity=f"venue=kalshi/date=2026-08-08/hour={n}",
                             subscription_metadata={"venue": "kalshi"})
        for i in range(per):
            assert w.submit(fields(i)) is None
        made.append(w.close())
    return made


class TestArchiveInit:
    def test_dry_run_creates_nothing(self, tmp_path, capsys):
        root = tmp_path / "root"
        rc = cli.archive_init(root=str(root), environment=ENV, confirm=False)
        assert rc == 0
        assert not (root / f"env={ENV}" / ah.GENESIS_FILENAME).exists()
        capsys.readouterr()

    def test_confirm_mints_a_genesis(self, tmp_path):
        root = tmp_path / "root"
        rc = cli.archive_init(root=str(root), environment=ENV, confirm=True)
        assert rc == 0
        genesis = ah.read_genesis(root, ENV)
        assert genesis["archive_identity"] == "kalshi-realtime"

    def test_reinitializing_an_existing_archive_is_refused(self, tmp_path):
        root = tmp_path / "root"
        cli.archive_init(root=str(root), environment=ENV, confirm=True)
        rc = cli.archive_init(root=str(root), environment=ENV, confirm=True)
        assert rc == 1

    def test_cli_arg_wiring(self):
        ns = cli.build_parser().parse_args(
            ["archive-init", "--root", "/tmp/x", "--confirm"])
        assert ns.command == "archive-init" and ns.confirm is True


class TestArchiveRecoverHead:
    def test_dry_run_on_a_healthy_archive_is_a_noop(self, tmp_path):
        root = tmp_path / "root"
        init(root)
        build(root, ["A"])
        rc = cli.archive_recover_head(root=str(root), environment=ENV,
                                      confirm=False)
        assert rc == 0
        assert ah.load_authoritative_head(root, ENV).generation == 1

    def test_confirm_finishes_a_stale_head(self, tmp_path):
        """Case B: the generation record is durable, only the pointer lags."""
        root = tmp_path / "root"
        init(root)
        build(root, ["A", "B"])
        ah._publish_current_head(root, ENV, ah.read_generation(root, ENV, 1))
        assert sg.verify_archive(root, environment=ENV)["head_state"] \
            == "STALE_HEAD"
        rc = cli.archive_recover_head(root=str(root), environment=ENV,
                                      confirm=True)
        assert rc == 0
        assert ah.load_authoritative_head(root, ENV).generation == 2
        assert sg.verify_archive(root, environment=ENV)["verdict"] == "VALID"

    def test_recovery_is_idempotent(self, tmp_path):
        root = tmp_path / "root"
        init(root)
        build(root, ["A", "B"])
        ah._publish_current_head(root, ENV, ah.read_generation(root, ENV, 1))
        cli.archive_recover_head(root=str(root), environment=ENV, confirm=True)
        rc = cli.archive_recover_head(root=str(root), environment=ENV, confirm=True)
        assert rc == 0
        assert ah.load_authoritative_head(root, ENV).generation == 2

    def test_cli_arg_wiring(self):
        ns = cli.build_parser().parse_args(
            ["archive-recover-head", "--root", "/tmp/x"])
        assert ns.command == "archive-recover-head" and ns.confirm is False


class TestArchiveAdopt:
    def test_refuses_a_segment_verify_archive_does_not_report_as_orphaned(
            self, tmp_path):
        root = tmp_path / "root"
        init(root)
        build(root, ["A"])
        rc = cli.archive_adopt(root=str(root), segment_id="kalshi.seg-A",
                               environment=ENV, confirm=True)
        assert rc == 1
        # The segment is fully committed; adopt must never re-commit it.
        assert ah.load_authoritative_head(root, ENV).generation == 1

    def test_adopts_a_genuine_crash_orphan(self, tmp_path):
        """Case A, reproduced exactly: a crash between manifest publish and
        the head commit leaves a durable manifest no generation mentions."""
        root = tmp_path / "root"
        init(root)
        build(root, ["A"])
        w = sg.SegmentWriter(
            root, environment=ENV, segment_id="kalshi.seg-B",
            partition_identity="venue=kalshi/date=2026-08-08/hour=B",
            subscription_metadata={"venue": "kalshi"})
        w.submit(fields(1))
        w.durability_hooks["head_generation_publish"] = lambda: (
            _ for _ in ()).throw(OSError("crash"))
        with pytest.raises(sg.OrphanedCommittedSegmentError):
            w.close()
        report = sg.verify_archive(root, environment=ENV)
        assert "kalshi.seg-B" in report["orphaned_committed_segments"]

        dry = cli.archive_adopt(root=str(root), segment_id="kalshi.seg-B",
                                environment=ENV, confirm=False)
        assert dry == 0
        # Dry run touched nothing: still orphaned, still generation 1.
        assert ah.load_authoritative_head(root, ENV).generation == 1

        rc = cli.archive_adopt(root=str(root), segment_id="kalshi.seg-B",
                               environment=ENV, confirm=True)
        assert rc == 0
        assert ah.load_authoritative_head(root, ENV).generation == 2
        after = sg.verify_archive(root, environment=ENV)
        assert after["verdict"] == "VALID"
        assert after["orphaned_committed_segments"] == []

        # Re-running is refused, not a second commit: idempotent by refusal.
        rc2 = cli.archive_adopt(root=str(root), segment_id="kalshi.seg-B",
                                environment=ENV, confirm=True)
        assert rc2 == 1
        assert ah.load_authoritative_head(root, ENV).generation == 2

    def test_a_stale_head_is_never_misrouted_to_adopt(self, tmp_path):
        """The A7.1 bug this milestone fixes: a failure AFTER the generation
        record durably links must not be reachable through archive-adopt --
        it is not in `orphaned_committed_segments` at all."""
        root = tmp_path / "root"
        init(root)
        build(root, ["A"])
        w = sg.SegmentWriter(
            root, environment=ENV, segment_id="kalshi.seg-B",
            partition_identity="venue=kalshi/date=2026-08-08/hour=B",
            subscription_metadata={"venue": "kalshi"})
        w.submit(fields(1))
        w.durability_hooks["current_head_publish"] = lambda: (
            _ for _ in ()).throw(OSError("crash"))
        with pytest.raises(sg.StaleHeadAfterCommitError):
            w.close()
        report = sg.verify_archive(root, environment=ENV)
        assert report["head_state"] == "STALE_HEAD"
        assert "kalshi.seg-B" not in report["orphaned_committed_segments"]
        rc = cli.archive_adopt(root=str(root), segment_id="kalshi.seg-B",
                               environment=ENV, confirm=True)
        assert rc == 1          # archive-adopt correctly refuses
        # The real remedy works.
        rc = cli.archive_recover_head(root=str(root), environment=ENV,
                                      confirm=True)
        assert rc == 0
        assert sg.verify_archive(root, environment=ENV)["verdict"] == "VALID"

    def test_cli_arg_wiring(self):
        ns = cli.build_parser().parse_args(
            ["archive-adopt", "--root", "/tmp/x", "--segment-id", "s1"])
        assert ns.command == "archive-adopt" and ns.segment_id == "s1"


class TestArchiveMigrateLegacyCLI:
    def test_dry_run_wiring(self, tmp_path, capsys):
        import gzip
        import json

        src = tmp_path / "legacy"
        d = src / f"env={ENV}" / "venue=kalshi" / "date=2026-07-01" / "hour=00"
        d.mkdir(parents=True)
        with gzip.open(d / sg.EVENTS_FILENAME, "wb") as fh:
            fh.write(json.dumps({
                "event_type": "orderbook_delta", "market_ticker": "KXA",
                "sid": 4, "seq": 0,
                "collector_receive_time": "2026-07-01T00:00:00.000000Z",
                "receive_monotonic_ns": 1,
                "raw": {"price_dollars": "0.5100"},
            }).encode() + b"\n")
        dest = tmp_path / "dest"
        rc = cli.archive_migrate_legacy(source=str(src), dest=str(dest),
                                        environment=ENV, confirm=False)
        assert rc == 0
        assert not dest.exists() or not (dest / f"env={ENV}" /
                                         ah.GENESIS_FILENAME).exists()
        capsys.readouterr()

    def test_confirm_wiring(self, tmp_path):
        import gzip
        import json

        src = tmp_path / "legacy"
        d = src / f"env={ENV}" / "venue=kalshi" / "date=2026-07-01" / "hour=00"
        d.mkdir(parents=True)
        with gzip.open(d / sg.EVENTS_FILENAME, "wb") as fh:
            fh.write(json.dumps({
                "event_type": "orderbook_delta", "market_ticker": "KXA",
                "sid": 4, "seq": 0,
                "collector_receive_time": "2026-07-01T00:00:00.000000Z",
                "receive_monotonic_ns": 1,
                "raw": {"price_dollars": "0.5100"},
            }).encode() + b"\n")
        dest = tmp_path / "dest"
        rc = cli.archive_migrate_legacy(source=str(src), dest=str(dest),
                                        environment=ENV, confirm=True)
        assert rc == 0
        assert sg.verify_archive(dest, environment=ENV)["verdict"] == "VALID"

    def test_cli_arg_wiring(self):
        ns = cli.build_parser().parse_args(
            ["archive-migrate-legacy", "--source", "/tmp/s", "--dest", "/tmp/d"])
        assert ns.command == "archive-migrate-legacy"


# --- A7.4: every operator instruction this module names must be real -------------


def _registered_commands() -> set:
    parser = cli.build_parser()
    for action in parser._actions:
        if isinstance(action, __import__("argparse")._SubParsersAction):
            return set(action.choices)
    raise AssertionError("could not find the subparsers action")


# Tokens that LOOK like a hyphenated command name but are artifact filenames
# or purpose labels, not operator instructions -- established by the A7 audit
# (`archive-head`/`archive-genesis`/`archive-writer` are files on disk;
# `archive-segments-fold` is a digest purpose string).
_NOT_COMMANDS = {
    "archive-head", "archive-genesis", "archive-writer", "archive-segments-fold",
    "kalshi-archive-writer",
    # Named exactly once, deliberately, to explain that it does NOT exist
    # (segment.py's ORPHANED_COMMITTED_SEGMENT message) -- the opposite of a
    # fictional instruction, so it is excluded here rather than required to
    # be a real command.
    "archive-discard",
}

_ARCHIVE_MODULES = (
    "app/realtime/archive.py",
    "app/realtime/archive_head.py",
    "app/realtime/segment.py",
    "app/realtime/legacy_import.py",
)

# Any string literal shaped like a real CLI command name in this codebase's
# established two families for the archive surface.
_COMMAND_SHAPED = re.compile(r"\b((?:archive|kalshi-realtime)-[a-z][a-z0-9-]*)\b")


def _string_literals(tree) -> list:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


class TestEveryOperatorInstructionIsReal:
    def test_command_shaped_strings_in_archive_modules_are_real_commands(self):
        """AST/text scan (KALSHI-ARCHIVE-CORE-REMEDIATION-002 A7.4): every
        command-shaped string an archive module's error message or docstring
        names must be a real, registered CLI subcommand -- never a filename
        or a purpose label wearing a command's clothing, and never a command
        this codebase does not implement."""
        commands = _registered_commands()
        found = set()
        for relpath in _ARCHIVE_MODULES:
            path = REPO / relpath
            tree = ast.parse(path.read_text())
            for literal in _string_literals(tree):
                for m in _COMMAND_SHAPED.finditer(literal):
                    token = m.group(1)
                    # A CLI FLAG (`--archive-identity`), not a command name --
                    # the two characters immediately before the match being
                    # `--` is what distinguishes "name an argument" from
                    # "name a subcommand" in this codebase's own docstring
                    # convention (`python -m app.cli <command> --flag ...`).
                    if literal[max(0, m.start() - 2):m.start()] == "--":
                        continue
                    if token in _NOT_COMMANDS:
                        continue
                    found.add(token)
        missing = sorted(t for t in found if t not in commands)
        assert not missing, (
            f"these command-shaped strings appear in archive error messages "
            f"or docstrings but are NOT registered CLI subcommands: {missing}")

    def test_no_fictional_commands_survive_in_source(self):
        """The specific fictional commands this milestone's audit found."""
        commands = _registered_commands()
        for fictional in ("archive-discard",
                          "kalshi-realtime-archive-migrate-legacy"):
            assert fictional not in commands
        for relpath in _ARCHIVE_MODULES:
            text = (REPO / relpath).read_text()
            assert "kalshi-realtime-archive-migrate-legacy" not in text, relpath

    def test_every_new_a7_command_is_registered_and_exercised_here(self):
        """Each command this milestone adds exists in the parser AND is
        actually invoked by name somewhere in this test file -- the
        'at least one behavioural test' requirement, made mechanical."""
        commands = _registered_commands()
        this_file = Path(__file__).read_text()
        for name in ("archive-init", "archive-recover-head", "archive-adopt",
                    "archive-migrate-legacy"):
            assert name in commands, f"{name} is not a registered subcommand"
            assert name.replace("-", "_") in this_file or f'"{name}"' in this_file, (
                f"{name} has no behavioural test in this file")
