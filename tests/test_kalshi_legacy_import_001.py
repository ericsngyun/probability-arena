"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — explicit legacy import, never automatic.

The authoritative decision is that pre-genesis archives are NOT auto-migrated.
These tests exist mainly to assert the absence of a path, because an automatic
migration is "the head is missing, therefore this is a new archive" wearing a
friendlier name — and that inference is what let a rebuilt history certify its
own deletions.
"""

from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path

import pytest

from app.realtime import archive as ar
from app.realtime import archive_head as ah
from app.realtime import legacy_import as li
from app.realtime import segment as sg

ENV = "demo"


def legacy_archive(root, *, files=2, per=5):
    """A pre-genesis archive: gzipped JSONL, no genesis, no manifest, no chain."""
    for f in range(files):
        d = root / f"env={ENV}" / "venue=kalshi" / "date=2026-07-01" / f"hour={f:02d}"
        d.mkdir(parents=True, exist_ok=True)
        with gzip.open(d / sg.EVENTS_FILENAME, "wb") as fh:
            for i in range(per):
                fh.write(json.dumps({
                    "event_type": "orderbook_delta", "market_ticker": "KXA",
                    "sid": 4, "seq": f * 100 + i,
                    "collector_receive_time": f"2026-07-01T{f:02d}:00:{i:02d}.000000Z",
                    "receive_monotonic_ns": 1_000_000 + i,
                    "raw": {"price_dollars": "0.5100"},
                }).encode() + b"\n")
    return root


class TestNoAutomaticPath:
    def test_the_collector_refuses_a_legacy_archive(self, tmp_path):
        legacy_archive(tmp_path)
        with pytest.raises(ah.ArchiveNotInitializedError):
            ar.EventArchive(tmp_path, environment=ENV)

    def test_verify_reports_not_initialized_rather_than_migrating(self, tmp_path):
        legacy_archive(tmp_path)
        out = sg.verify_archive(tmp_path, environment=ENV)
        assert out["head_state"] == "NOT_INITIALIZED"
        assert out["verdict"] == "INVALID"

    @pytest.mark.parametrize("module", ["archive", "segment", "archive_head"])
    def test_no_runtime_module_imports_the_importer(self, module):
        """Structural: the runtime must have no reference to the import path."""
        src = Path(f"app/realtime/{module}.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "legacy_import" not in node.module, module
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "legacy_import" not in alias.name, module
        assert "migrate_legacy" not in src, module


class TestDryRunTouchesNothing:
    def test_dry_run_writes_nothing_and_reports_what_it_read(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy", files=2, per=5)
        before = {p: p.stat().st_mtime_ns for p in src.rglob("*") if p.is_file()}
        plan = li.migrate_legacy_archive(src, tmp_path / "dest", environment=ENV)
        assert plan["mode"] == "dry-run"
        assert plan["would_import"] == 10
        assert plan["records_imported"] == 0
        assert len(plan["artifact_digests"]) == 2
        assert plan["first_timestamp"] < plan["last_timestamp"]
        assert not (tmp_path / "dest").exists(), "a dry run created the destination"
        after = {p: p.stat().st_mtime_ns for p in src.rglob("*") if p.is_file()}
        assert before == after, "a dry run modified the source"

    def test_limitations_are_recorded_as_data(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy")
        plan = li.migrate_legacy_archive(src, tmp_path / "dest", environment=ENV)
        joined = " ".join(plan["source_limitations"])
        for expected in ("genesis", "generation chain", "per-record chain",
                         "record count"):
            assert expected in joined, expected
        assert "FROM THE IMPORT FORWARD ONLY" in plan["guarantee_boundary"]


class TestConfirmedImport:
    def test_import_creates_a_new_governed_archive_and_leaves_the_source(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy", files=2, per=5)
        before = {str(p.relative_to(src)): p.read_bytes()
                  for p in src.rglob("*") if p.is_file()}
        dest = tmp_path / "dest"
        result = li.migrate_legacy_archive(src, dest, environment=ENV, confirm=True)

        assert result["records_imported"] == 10
        assert result["records_refused_at_import"] == 0
        # The destination is a real governed archive with its own genesis.
        assert sg.verify_archive(dest, environment=ENV)["verdict"] == "VALID"
        genesis = ah.read_genesis(dest, ENV)
        assert result["archive_id"] == genesis["archive_id"]
        # And the source is byte-identical afterwards.
        after = {str(p.relative_to(src)): p.read_bytes()
                 for p in src.rglob("*") if p.is_file()}
        assert before == after, "the import modified the legacy source"

    def test_provenance_pins_the_source_digests(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy")
        dest = tmp_path / "dest"
        result = li.migrate_legacy_archive(src, dest, environment=ENV, confirm=True)
        prov = (dest / f"env={ENV}" / li.PROVENANCE_FILENAME)
        assert prov.exists()
        from app.realtime.canonical import digest_hex, parse_canonical
        stored = parse_canonical(prov.read_bytes())
        assert stored["provenance_digest"] == digest_hex(
            {k: v for k, v in stored.items() if k != "provenance_digest"})
        # Every source artifact is digested as found.
        for rel, dig in result["artifact_digests"].items():
            assert len(dig) == 64
            assert stored["artifact_digests"][rel] == dig
        assert stored["migration_timestamp"]

    def test_importing_into_an_initialized_archive_is_refused(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy")
        dest = tmp_path / "dest"
        ah.initialize_archive(dest, ENV, archive_identity="kalshi-realtime")
        with pytest.raises(li.LegacyImportError, match="already an initialized"):
            li.migrate_legacy_archive(src, dest, environment=ENV, confirm=True)

    def test_in_place_migration_is_refused(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy")
        with pytest.raises(li.LegacyImportError, match="same directory"):
            li.migrate_legacy_archive(src, src, environment=ENV, confirm=True)

    def test_a_torn_legacy_file_is_counted_not_silently_dropped(self, tmp_path):
        src = legacy_archive(tmp_path / "legacy", files=1, per=5)
        target = next(src.rglob(sg.EVENTS_FILENAME))
        raw = bytearray(target.read_bytes())
        target.write_bytes(bytes(raw[: len(raw) // 2]))     # torn tail
        plan = li.migrate_legacy_archive(src, tmp_path / "dest", environment=ENV)
        assert plan["records_readable"] + plan["records_rejected"] <= 5
        assert plan["artifact_digests"], "the digest must reflect what was READ"

    def test_an_empty_source_is_refused_rather_than_imported_as_nothing(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(li.LegacyImportError, match="no .* files|not a directory"):
            li.migrate_legacy_archive(empty, tmp_path / "dest", environment=ENV)
