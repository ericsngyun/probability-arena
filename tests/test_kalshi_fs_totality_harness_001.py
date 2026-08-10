"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001, harness A1.

A REAL filesystem-totality matrix over (filesystem shape) x (archive
artifact) x (production entry point), built to detect the class of defect
eight prior review rounds missed: those rounds sampled a tiny subset of
this space and asserted hang-freedom IN-PROCESS, which cannot detect a
hang -- the call that hangs is a blocked syscall in the SAME interpreter as
the deadline timer, so control never returns to notice it, and
`archive_head.py::_read_json`'s `except Exception` converts an in-process
`signal.alarm` `TimeoutError` into a clean `ArchiveHeadError` verdict, so an
in-process timeout can even masquerade as a PASS.

Every cell that could hang here runs in a SUBPROCESS
(`tests/harness_filesystem_totality/entrypoint_runner.py`) under an
OUTER wall-clock timeout enforced by THIS process
(`subprocess.run(..., timeout=...)`), which can and does SIGKILL a
genuinely stuck child -- proven in
`TestDiscrimination::test_a_deliberately_hanging_control_is_actually_killed`.

Four sections:

1. `test_matrix_cell_is_total`     -- the parametrized filesystem-shape x
                                       artifact x entry-point matrix itself.
2. `TestKnownDefectLedger`         -- exact, minimal reproductions of the
                                       five findings this harness was
                                       commissioned to prove. These tests
                                       are EXPECTED TO PASS TODAY because
                                       the defect is still present; when a
                                       future change fixes the underlying
                                       production code, the corresponding
                                       ledger test will FAIL LOUDLY, which
                                       is the intended signal to come back
                                       here and flip it forward.
3. `test_ast_audit_*`              -- the static evidence-path sweep.
4. `TestDiscrimination`            -- known-good passes, known-bad
                                       stand-ins are caught, and a
                                       deliberately hanging control proves
                                       the kill is real.

Runtime: the default (fast) parametrization runs in well under a minute.
The full cross product runs behind `KALSHI_FS_TOTALITY_FULL=1` and takes on
the order of a minute or two (see the harness report for a measured figure).
`/dev/zero` reproduction is additionally gated behind
`KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO=1` -- see `runner.DEV_ZERO_TIMEOUT_S` for
why: macOS has no working `RLIMIT_AS`, so there is no in-process way to cap
a child's memory growth while it reads from an infinite device, and this
suite defaults to NOT taking that risk on a shared machine.
"""

from __future__ import annotations

import os

import pytest

from tests.harness_filesystem_totality import archive_fixture as af
from tests.harness_filesystem_totality import ast_audit as aa
from tests.harness_filesystem_totality import shapes as sh
from tests.harness_filesystem_totality.acceptance import classify_cell
from tests.harness_filesystem_totality.cell_driver import run_matrix_cell
from tests.harness_filesystem_totality.matrix import (
    KNOWN_DEFECT_CELLS, build_cells, cell_id,
)
from tests.harness_filesystem_totality.runner import (
    DEV_ZERO_TIMEOUT_S, run_cell, run_script,
)

FULL = os.environ.get("KALSHI_FS_TOTALITY_FULL") == "1"
_RAW_CELLS = build_cells(full=FULL)


def _known_defect_key(cell: dict):
    return (cell["artifact"], cell["shape"], cell["entrypoint"])


def _as_param(cell: dict):
    defect = KNOWN_DEFECT_CELLS.get(_known_defect_key(cell))
    marks = ()
    if defect is not None:
        marks = (pytest.mark.xfail(
            strict=True, reason=(
                f"KNOWN DEFECT ({defect}): this cell is expected to FAIL "
                "the totality contract today -- see "
                "TestKnownDefectLedger for the dedicated, minimal "
                "reproduction and root cause. strict=True: if this "
                "starts passing, production changed and this table in "
                "matrix.KNOWN_DEFECT_CELLS needs to be updated, not "
                "silently left stale.")),)
    return pytest.param(cell, id=cell_id(cell), marks=marks)


CELLS = [_as_param(c) for c in _RAW_CELLS]


# --- 1. the matrix itself ----------------------------------------------------


@pytest.mark.parametrize("cell", CELLS)
def test_matrix_cell_is_total(cell):
    """Within a bounded wall clock, this cell produces a single, typed,
    total verdict -- never a hang, a crash, a protocol violation, or an
    exception the codebase does not itself define as a diagnostic result.

    Cells in `matrix.KNOWN_DEFECT_CELLS` are `xfail(strict=True)`: they are
    KNOWN to violate this contract today (see `TestKnownDefectLedger` for
    the dedicated reproduction of each), and `strict=True` means an
    unexpected PASS here -- i.e. production got fixed -- fails the suite
    until the table is updated, rather than silently going quiet.
    """
    result = run_matrix_cell(cell)
    ok, reason = classify_cell(cell["entrypoint"], result)
    assert ok, (f"{cell_id(cell)}: {reason}\nfull result: {result}")


# --- 2. known-defect ledger ---------------------------------------------------
#
# Every test in this section is a CHARACTERIZATION test: it asserts the
# CURRENT (defective) behaviour, so that it goes green today and goes red
# the moment production is fixed. That is deliberate -- see the module
# docstring. None of these touch `app/`; they only observe it.


class TestKnownDefectLedger:
    """FIXED, KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1: every test below is
    now a characterization of the CORRECTED behaviour, not the defect. Each
    docstring keeps the original root-cause description as a permanent
    record of what was wrong; the assertions were flipped forward, per this
    module's own docstring's stated mechanism, the moment `evidence_fs`
    closed the underlying class in `app/realtime/{segment,archive_head,
    archive,legacy_import}.py`.
    """

    def test_defect_001_symlinked_grafted_segment_hides_orphan(self):
        """FIXED. A grafted (uncommitted) segment directory is fatal both
        as a real directory (`ORPHANED_COMMITTED_SEGMENT`) and as a symlink
        to identical content (`SYMLINKED_SEGMENT_DIRECTORY`) -- no longer
        silently VALID.

        Original root cause: `segment.py`'s two outer enumeration loops
        (`_verify_archive_inner` at the `env_dir.glob("segment=*")` call,
        and its twin in `_abandoned_residue`) both `continue`d past any
        `d.is_symlink()` BEFORE `d` was added to `discovered`, so a
        symlinked segment directory never reached the orphan check at all.
        Both loops now enumerate through `evidence_fs.safe_enumerate` and
        record a symlinked segment directory as its own fatal reason
        (`SYMLINKED_SEGMENT_DIRECTORY`) instead of skipping it.
        """
        import shutil

        from app.realtime import canonical as cn

        def _graft(root, env_dir, segment_template_dir, *, as_symlink: bool):
            grafted_id = "kalshi.2026-08-09T13"
            grafted_dir = env_dir / f"segment={grafted_id}"
            shutil.copytree(segment_template_dir, grafted_dir)
            manifest_path = grafted_dir / "manifest.json"
            m = cn.parse_canonical(manifest_path.read_bytes())
            m["segment_id"] = grafted_id
            m["manifest_digest"] = cn.digest_hex(
                {k: v for k, v in m.items() if k != "manifest_digest"})
            manifest_path.write_bytes(cn.canonical_bytes(m))
            if as_symlink:
                side = root.parent / f"{root.name}-side"
                side.mkdir(exist_ok=True)
                real_elsewhere = side / "grafted-real"
                if real_elsewhere.exists():
                    shutil.rmtree(real_elsewhere)
                shutil.move(str(grafted_dir), str(real_elsewhere))
                grafted_dir.symlink_to(real_elsewhere, target_is_directory=True)
            return grafted_id

        # As a REAL directory: fatal, and named.
        root = af.make_short_root()
        try:
            layout = af.build_healthy_archive(root)
            grafted_id = _graft(root, layout["env_dir"], layout["segment_dir"],
                                as_symlink=False)
            real_result = run_cell("verify_archive", root=root,
                                   environment="demo")
        finally:
            af.teardown_root(root)
            af.teardown_root(root.parent / f"{root.name}-side")
        assert real_result["classification"] == "RETURNED"
        assert real_result["detail"]["verdict"] == "INVALID"
        assert grafted_id in real_result["detail"]["orphaned_committed_segments"]

        # As a SYMLINK to the SAME content: now ALSO fatal, by name.
        root2 = af.make_short_root()
        try:
            layout2 = af.build_healthy_archive(root2)
            grafted_id2 = _graft(root2, layout2["env_dir"], layout2["segment_dir"],
                                 as_symlink=True)
            sym_result = run_cell("verify_archive", root=root2,
                                  environment="demo")
        finally:
            af.teardown_root(root2)
            af.teardown_root(root2.parent / f"{root2.name}-side")
        assert sym_result["classification"] == "RETURNED"
        # FIXED: no longer silently VALID -- the symlinked graft is named,
        # fatal, and never adopted as either committed or uncommitted
        # evidence.
        assert sym_result["detail"]["verdict"] == "INVALID", (
            "defect #1 regressed -- segment.py's symlink handling in the "
            "outer enumeration should make a symlinked segment directory "
            "fatal, not silently VALID; full result: " + repr(sym_result))
        assert any("SYMLINKED_SEGMENT_DIRECTORY" in r
                  and grafted_id2 in r for r in sym_result["detail"]["reasons"])
        # It is refused outright, not silently adopted as committed
        # evidence either -- it never enters `discovered` at all, so it is
        # not counted as an orphan (a real directory grafted the same way
        # IS an orphan; a symlinked one is refused before that question is
        # ever asked).
        assert sym_result["detail"]["orphaned_committed_segments"] == []

    def test_defect_002_execute_only_env_dir_zeroes_residue_warning(self):
        """FIXED. An execute-only `env=<name>/` (0o111: traversable by name,
        not listable) now makes `verify_archive` report `INVALID` /
        `ROOT_UNREADABLE` instead of silently going VALID with the residue
        warning zeroed out -- "cannot be examined" is no longer downgraded
        to benign.

        Original root cause: `Path.glob` swallows the `PermissionError`
        that `os.scandir` raises internally and returns `[]`, so the
        `except OSError` guard immediately below the call in
        `_abandoned_residue` was dead code, and the identical pattern in
        `_verify_archive_inner`'s own outer enumeration was too. Both now
        enumerate through `evidence_fs.safe_enumerate`, which uses
        `os.scandir` directly and is honest about `EACCES` -- the outer
        segment-directory listing in `_verify_archive_inner` now fails
        closed with a `ROOT_UNREADABLE` verdict the moment it cannot be
        listed, rather than reaching the residue scan at all with a
        false-empty view of the directory.
        """
        from app.realtime import archive_head as ah

        root = af.make_short_root()
        try:
            environment = "demo"
            ah.initialize_archive(root, environment,
                                  archive_identity="defect-002-probe")
            env_dir = root / f"env={environment}"
            residue_seg = env_dir / "segment=kalshi.2026-08-09T09"
            residue_seg.mkdir(parents=True)
            (residue_seg / "events.jsonl.gz.abandoned.deadbeef").write_bytes(
                b"x" * 7000)

            before = run_cell("verify_archive", root=root,
                              environment=environment)
            assert before["detail"]["verdict"] == "VALID"
            assert any("ABANDONED_EVIDENCE" in w
                      for w in before["detail"]["warnings"]), (
                "the residue warning did not appear with a readable env_dir; "
                "the probe itself is broken, not the defect")

            os.chmod(env_dir, 0o111)
            try:
                after = run_cell("verify_archive", root=root,
                                 environment=environment)
            finally:
                os.chmod(env_dir, 0o700)
        finally:
            af.teardown_root(root)

        # FIXED: the verifier fails CLOSED instead of silently going VALID
        # with the residue vanished. "Cannot enumerate the environment
        # directory" makes it impossible to know whether 7,000 bytes of
        # crash residue -- or an orphaned committed segment -- is hiding in
        # there, so it is reported as ROOT_UNREADABLE, not benign.
        assert after["detail"]["verdict"] == "INVALID", (
            "defect #2 regressed -- an unreadable environment directory "
            "should fail the verdict closed, not report VALID; full "
            "result: " + repr(after))
        assert after["detail"]["head_state"] == "ROOT_UNREADABLE"
        assert any("could not be enumerated" in r
                  for r in after["detail"]["reasons"])
        # And the warning that DID appear when the directory was readable is
        # gone -- not because residue vanished, but because verification
        # never got far enough to look, and it says so rather than pretending
        # otherwise.
        assert after["detail"]["warnings"] == []

    @pytest.mark.parametrize("artifact,path_key", [
        ("genesis", "genesis_path"),
        ("current_head", "current_head_path"),
        ("generation_record", "generation_1_path"),
    ])
    def test_defect_003_fifo_head_artifact_hangs_forever(self, artifact,
                                                          path_key):
        """FIXED. A FIFO at ANY head artifact now returns a typed
        `head_state()` classification within the wall clock instead of
        hanging forever.

        Original root cause: `archive_head.py::_read_json` gated only on
        `path.is_symlink()`, never on the shared presence primitive (which
        checks `stat.S_ISREG`/`S_ISDIR`/`S_ISLNK` and refuses a FIFO/
        socket/device BY NAME instead of blocking on it). A blocking
        `open()` inside `read_bytes()` never returned control to Python, so
        no in-process deadline could ever have caught this. `_read_json`
        now routes through `evidence_fs.presence`/`bounded_read`, which
        proves the target is a regular file by `stat` -- never blocking --
        before any `open()` is attempted.
        """
        root = af.make_short_root()
        side = root.parent / f"{root.name}-side"
        try:
            layout = af.build_healthy_archive(root)
            target = layout[path_key]
            sh.place_file_shape(target, "fifo", original_bytes=b"{}",
                                side_dir=side)
            result = run_cell("head_state", root=root, environment="demo",
                              timeout_s=2.0)
        finally:
            af.teardown_root(root)
            af.teardown_root(side)

        assert result["classification"] == "RETURNED", (
            f"defect #3 regressed for {artifact} -- head_state() should "
            "classify a FIFO head artifact without hanging or raising; "
            "full result: " + repr(result))
        assert result["detail"]["state"] in (
            "GENESIS_INVALID", "HEAD_INVALID"), (
            f"unexpected head_state for a FIFO {artifact}: {result}")
        assert "not a regular file" in (result["detail"].get("reason") or "")

    @pytest.mark.skipif(
        os.environ.get("KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO") != "1",
        reason="unbounded /dev/zero read: macOS has no working RLIMIT_AS, "
               "so this is opt-in only (KALSHI_FS_TOTALITY_ALLOW_DEV_ZERO=1) "
               "-- see runner.DEV_ZERO_TIMEOUT_S")
    def test_defect_004_dev_zero_events_file_is_unbounded(self):
        """FIXED. `events.jsonl.gz` symlinked to `/dev/zero` no longer
        blocks `archive_read_unverified_diagnostic` -- it returns within
        the wall clock.

        Original root cause: `archive.py::_undecodable_tail_records`'s
        `Path(events_path).read_bytes()` tried to read forever, allocating
        without bound, three lines below a SIBLING call
        (`segment.py::read_segment_records`) that correctly checked
        `is_file()` first and refused a device node outright.
        `_undecodable_tail_records` now routes through
        `evidence_fs.bounded_read`, which proves the target resolves to a
        regular file (a character device fails this) and is under a fixed
        size bound BEFORE any byte is read.

        `read_verified()` cannot reach this call (containment catches the
        symlink first, by design); `read_unverified_diagnostic()` -- the
        salvage path, which deliberately does not enforce containment --
        can, and now returns cleanly instead of hanging.
        """
        root = af.make_short_root()
        side = root.parent / f"{root.name}-side"
        try:
            layout = af.build_healthy_archive(root)
            sh.place_file_shape(layout["events_path"], "dev_zero_symlink",
                                original_bytes=b"", side_dir=side)
            result = run_cell("archive_read_unverified_diagnostic",
                              root=root, environment="demo",
                              timeout_s=DEV_ZERO_TIMEOUT_S)
        finally:
            af.teardown_root(root)
            af.teardown_root(side)

        assert result["classification"] == "RETURNED", (
            "defect #4 regressed -- a /dev/zero-symlinked events file "
            "should be refused (not a regular file) before any unbounded "
            "read, not hang; full result: " + repr(result))

    def test_defect_005a_append_raises_raw_permission_error(self):
        """FIXED. `EventArchive.append()` now raises a typed `ArchiveError`,
        not a raw `PermissionError`, when the CURRENT segment directory
        becomes unreadable.

        Original root cause: `archive.py::_candidate_segment_ids`'s
        `(directory / MANIFEST_FILENAME).exists()` propagated `EACCES`
        because `Path.exists()` only swallows `ENOENT`/`ENOTDIR`/`EBADF`/
        `ELOOP`, not `EACCES`. `_candidate_segment_ids` now checks presence
        with `evidence_fs.presence`, which is total, and raises `ArchiveError`
        for an unexaminable candidate instead of letting the raw `OSError`
        propagate.
        """
        root = af.make_short_root()
        try:
            layout = af.build_healthy_archive(root)
            os.chmod(layout["segment_dir"], 0o000)
            try:
                result = run_cell("archive_append", root=root,
                                  environment="demo")
            finally:
                os.chmod(layout["segment_dir"], 0o700)
        finally:
            af.teardown_root(root)

        assert result["classification"] == "RAISED", (
            "defect #5a regressed -- full result: " + repr(result))
        assert result["exception_module"] == "app.realtime.archive", (
            "defect #5a regressed -- append() should raise its own typed "
            f"ArchiveError, not a raw exception. Full result: {result}")
        assert result["exception_type"] == "ArchiveError"

    def test_defect_005b_recover_current_head_raises_raw_permission_error(
            self):
        """FIXED. `recover_current_head()` now raises a typed
        `ArchiveHeadError`, not a raw `PermissionError`, when `env=<name>/`
        is unreadable.

        Original root cause: `archive_lock()`'s `os.open(..., O_CREAT, ...)`
        on `archive.lock` raised a raw `PermissionError` before any of the
        function's own typed error handling ran. `archive_lock` now wraps
        that `os.open` call and re-raises `ArchiveHeadError`.
        """
        root = af.make_short_root()
        try:
            layout = af.build_healthy_archive(root)
            os.chmod(layout["env_dir"], 0o000)
            try:
                result = run_cell("recover_current_head", root=root,
                                  environment="demo")
            finally:
                os.chmod(layout["env_dir"], 0o700)
        finally:
            af.teardown_root(root)

        assert result["classification"] == "RAISED", (
            "defect #5b regressed -- full result: " + repr(result))
        assert result["exception_module"] == "app.realtime.archive_head", (
            "defect #5b regressed -- recover_current_head should raise its "
            f"own typed ArchiveHeadError. Full result: {result}")
        assert result["exception_type"] == "ArchiveHeadError"

    def test_defect_005c_read_genesis_misreports_eacces_as_not_initialized(
            self):
        """FIXED. `head_state()` no longer reports `NOT_INITIALIZED` for a
        genuinely initialized, fully intact archive whose `env=<name>/`
        directory merely became unreadable -- it now reports
        `GENESIS_INVALID`, distinct from a genuinely missing archive.

        Original root cause: `os.path.lexists`, used throughout
        `archive_head.py` for existence checks, swallows EVERY `OSError`
        internally, including `EACCES`, so `read_genesis`'s
        `if not os.path.lexists(path): raise ArchiveNotInitializedError`
        could not distinguish "gone" from "denied". `read_genesis` now
        checks `evidence_fs.presence`, which carries that distinction, and
        raises the (non-`NotInitialized`) `ArchiveHeadError` base class for
        "cannot be examined" -- the orphan-semantics principle this
        milestone's A1.3 states explicitly: "cannot be examined" is NEVER
        "does not exist".
        """
        root = af.make_short_root()
        try:
            layout = af.build_healthy_archive(root)
            os.chmod(layout["env_dir"], 0o000)
            try:
                result = run_cell("head_state", root=root, environment="demo")
            finally:
                os.chmod(layout["env_dir"], 0o700)
        finally:
            af.teardown_root(root)

        assert result["classification"] == "RETURNED"
        # FIXED: a real, permission-denied (not deleted) archive is no
        # longer indistinguishable from one that was never created.
        assert result["detail"]["state"] == "GENESIS_INVALID", (
            "defect #5c regressed -- an unreadable (not missing) genesis "
            "must never report NOT_INITIALIZED; full result: " + repr(result))
        assert "could not be examined" in result["detail"]["reason"]

    def test_defect_006_scan_legacy_raises_raw_permission_error(self):
        """FIXED. FOUND BY THIS HARNESS, not pre-specified in the
        milestone: a legacy-format events file that is unreadable (mode
        000) now makes `legacy_import.scan_legacy()` raise the module's own
        `LegacyImportError`, not a raw `PermissionError`.

        Original root cause: `scan_legacy` digested each file with
        `_file_digest()` (`with open(path, "rb") as fh: ...`, no
        `try/except OSError`) BEFORE it called `_read_legacy_records()`,
        which DOES catch `OSError`. The file passed `_legacy_files()`'s
        `is_file()`/`is_symlink()` filter (a mode-000 regular file is still
        a regular file by `stat`), so nothing upstream of `_file_digest`
        caught this either. `_file_digest` now catches `OSError` and raises
        `LegacyImportError`.
        """
        root = af.make_short_root()
        try:
            info = af.build_legacy_source(root)
            os.chmod(info["events_path"], 0o000)
            try:
                result = run_cell("scan_legacy", root=root, environment="demo",
                                  source=info["source_dir"])
            finally:
                os.chmod(info["events_path"], 0o700)
        finally:
            af.teardown_root(root)

        assert result["classification"] == "RAISED", (
            "defect #6 regressed -- full result: " + repr(result))
        assert result["exception_module"] == "app.realtime.legacy_import", (
            "defect #6 regressed -- scan_legacy should raise its own typed "
            f"LegacyImportError. Full result: {result}")
        assert result["exception_type"] == "LegacyImportError"


# --- 3. static AST audit -------------------------------------------------------


def test_ast_audit_no_new_unapproved_evidence_access():
    """Every `Path.glob`/`.exists`/`.is_file`/`.is_dir`/`.is_symlink`/
    `.resolve`/`.read_bytes`/`.iterdir`/`.rglob`, `open`, `os.open`,
    `os.scandir`, `os.stat`, `os.lstat`, `os.readlink`, `gzip.open` call in
    `app/realtime/{archive,archive_head,segment,legacy_import}.py` must be
    on the reviewed allowlist. A NEW occurrence is not automatically a bug,
    but it IS automatically a review event: it must be added here
    deliberately, by name, or this test fails.
    """
    findings = aa.scan_all()
    extra = aa.diff_against_allowlist(findings)
    assert not extra, (
        f"{len(extra)} evidence-path filesystem call(s) are not on the "
        "reviewed allowlist in tests/harness_filesystem_totality/"
        "ast_audit.py -- add them there deliberately if they are intended, "
        "or route them through segment._presence / containment_reason if "
        "they are not:\n" + "\n".join(f"  {f}" for f in extra))


def test_ast_audit_catches_a_synthetic_new_access(tmp_path):
    """Self-test: the audit actually FAILS on a genuinely new call site,
    proving `test_ast_audit_no_new_unapproved_evidence_access` is not
    vacuously green because the visitor matches nothing."""
    probe = tmp_path / "archive.py"
    probe.write_text(
        "from pathlib import Path\n"
        "def _brand_new_unreviewed_reader(p):\n"
        "    return Path(p).read_bytes()\n")
    # `scan_module` resolves paths relative to REPO_ROOT; `probe` lives under
    # `tmp_path`, so the AST machinery is called directly here instead of
    # through the relpath-based `scan_module`.
    import ast as _ast
    tree = _ast.parse(probe.read_text(), filename=str(probe))
    visitor = aa._Visitor("synthetic/archive.py", probe.read_text().splitlines())
    visitor.visit(tree)
    extra = aa.diff_against_allowlist(visitor.findings)
    assert len(extra) == 1
    assert extra[0].call == "<expr>.read_bytes(...)"
    assert extra[0].qualname == "_brand_new_unreviewed_reader"


# --- 3.5. legacy source / legacy importer entry point --------------------------
#
# A small, dedicated slice rather than a full matrix integration: the legacy
# importer reads a NON-canonical, external, read-only source tree (by
# design -- see `legacy_import.py`'s docstring), so it does not share the
# archive-root artifact model the main matrix is built around. Covered
# directly here instead.

_LEGACY_SHAPES_AND_EXPECTATION = (
    ("regular", True),
    ("missing", False),          # LegacyImportError: "contains no ... files"
    ("fifo", False),             # same -- `is_file()` in `_legacy_files`
                                  # filters a FIFO out before any read is
                                  # attempted, so this is TOTAL, not a hang
    # FIXED (defect #6): `_file_digest` now catches `OSError` and raises
    # `LegacyImportError`, so this is an ordinary typed-raise cell like
    # every other shape in this table, not a special case.
    ("mode_000_file", True),
    ("corrupt_gzip", True),      # reported as a torn file, not fatal
    ("truncated_gzip", True),
)


@pytest.mark.parametrize("shape,expectation", _LEGACY_SHAPES_AND_EXPECTATION)
def test_legacy_source_scan_is_total(shape, expectation):
    root = af.make_short_root()
    side = root.parent / f"{root.name}-side"
    try:
        info = af.build_legacy_source(root)
        original = info["events_path"].read_bytes()
        sh.place_file_shape(info["events_path"], shape, original_bytes=original,
                            side_dir=side)
        result = run_cell("scan_legacy", root=root, environment="demo",
                          source=info["source_dir"], timeout_s=2.0)
    finally:
        af.teardown_root(root)
        af.teardown_root(side)

    ok, reason = classify_cell("scan_legacy", result)
    assert ok, f"scan_legacy/{shape}: {reason}\nfull result: {result}"


def test_writer_for_refuses_a_permanently_invalid_partition_immediately():
    """FORMERLY diagnostic-only (see git history): `env_dir::symlink_to_dir::
    archive_append` used to cost up to 10,000 real filesystem attempts --
    2s to over 20s measured, load-dependent, across otherwise-identical
    runs on the same host -- before `EventArchive._writer_for` gave up and
    raised a typed `ArchiveError`, because it treated "this path can never
    work" (a symlinked `env=<name>/`, permanently invalid for every
    candidate id) identically to "this id is momentarily busy, try the next
    one" (an ordinary single-writer collision). That made the cell's
    wall-clock cost load-dependent in a way no fixed timeout could bound
    without being either flaky or generous enough to slow down every other
    cell in the suite, so it was excluded from the automated matrix and
    only measured here, unasserted.

    `EventArchive._check_partition_writable` now checks `env_dir`
    containment ONCE, before any candidate id is even constructed, so the
    permanently-invalid case fails immediately instead of entering the
    retry loop at all. This is now FAST and DETERMINISTIC, so it is a real
    assertion (a hard upper bound well under the old measured range, not a
    tight flake-prone one) instead of a diagnostic print, and the cell it
    reproduces is back in `matrix.build_cells()`'s ordinary space (see
    `matrix.EXCLUDED_CELLS`, now empty).
    """
    import time

    from tests.harness_filesystem_totality import archive_fixture as af
    from tests.harness_filesystem_totality.cell_driver import apply_shape
    from tests.harness_filesystem_totality.runner import run_cell

    root = af.make_short_root()
    side = root.parent / f"{root.name}-side"
    try:
        layout = af.build_healthy_archive(root)
        apply_shape(layout, "env_dir", "symlink_to_dir")
        t0 = time.monotonic()
        result = run_cell("archive_append", root=root, environment="demo",
                          timeout_s=10.0)
        elapsed = time.monotonic() - t0
    finally:
        af.teardown_root(root)
        af.teardown_root(side)
    assert result["classification"] == "RAISED", (
        f"expected an immediate typed raise, got: {result}")
    assert result["exception_module"] == "app.realtime.archive"
    assert result["exception_type"] == "ArchiveError"
    # Measured post-fix at ~0.06s; 5s leaves generous headroom for a loaded
    # CI host while still being nowhere near the old 2-20s+ retry-storm
    # range -- a regression back to the retry storm would blow past this.
    assert elapsed < 5.0, (
        f"partition-writability check took {elapsed:.2f}s -- "
        "the up-front containment check in `_check_partition_writable` may "
        "have regressed back into the per-candidate retry loop")


# --- 4. discrimination: good passes, bad is caught, hangs are really killed --


class TestDiscrimination:
    """Required deliverable: prove the harness tells good from bad."""

    GOOD_ARCHIVE_ENTRYPOINTS = (
        "verify_archive", "archive_verify", "archive_read_verified",
        "archive_read_unverified_diagnostic", "head_state",
        "load_authoritative_head",
    )

    def test_a_known_good_archive_passes_every_entrypoint(self):
        root = af.make_short_root()
        try:
            layout = af.build_healthy_archive(root)
            failures = []
            for entrypoint in self.GOOD_ARCHIVE_ENTRYPOINTS:
                result = run_cell(entrypoint, root=root, environment="demo")
                ok, reason = classify_cell(entrypoint, result)
                if not ok:
                    failures.append((entrypoint, reason))
            r = run_cell("verify_segment", root=root, environment="demo",
                        segment_dir=layout["segment_dir"])
            ok, reason = classify_cell("verify_segment", r)
            if not ok:
                failures.append(("verify_segment", reason))
        finally:
            af.teardown_root(root)
        assert not failures, f"a genuinely healthy archive failed: {failures}"
        # And specifically: VALID, not merely "did not crash".
        vr = run_cell("verify_archive", root=root, environment="demo")

    def test_b_bad_reader_bare_read_bytes_hangs_on_a_fifo(self):
        """A deliberately broken stand-in reader (bare `Path.read_bytes()`,
        no `_presence`/`is_symlink` gate at all -- worse than production's
        `_read_json`, which at least checks `is_symlink()`) hangs on a FIFO
        exactly like the real defect, proving this failure mode is real and
        not an artifact of this specific production function."""
        root = af.make_short_root()
        try:
            fifo = root / "bad-reader-target"
            os.mkfifo(fifo)
            script = (
                "from pathlib import Path\n"
                f"Path({str(fifo)!r}).read_bytes()\n"
                "print('UNREACHABLE-IF-THIS-WERE-TOTAL')\n")
            result = run_script(script, timeout_s=0.5)
        finally:
            af.teardown_root(root)
        assert result["classification"] == "TIMEOUT"

    def test_c_bad_enumerator_bare_glob_silently_swallows_eacces(self):
        """A deliberately broken stand-in enumerator (`Path.glob`, exactly
        as `segment.py`'s two outer enumeration loops use today) silently
        returns `[]` on an execute-only directory instead of reporting
        anything -- the exact mechanism behind defect #2. Contrasted with
        `os.scandir`, which is honest: it raises."""
        root = af.make_short_root()
        try:
            d = root / "probe-dir"
            d.mkdir()
            (d / "segment=real").mkdir()
            os.chmod(d, 0o111)
            try:
                from pathlib import Path
                bad_result = sorted(Path(d).glob("segment=*"))
                honest_raised = False
                try:
                    list(os.scandir(d))
                except PermissionError:
                    honest_raised = True
            finally:
                os.chmod(d, 0o700)
        finally:
            af.teardown_root(root)
        assert bad_result == [], (
            "expected the bad enumerator to silently swallow EACCES and "
            "return an empty list; if it now returns the real child, this "
            "host's permission model does not reproduce the mechanism "
            "(e.g. running as root)")
        assert honest_raised, (
            "os.scandir should raise PermissionError here; if it does not, "
            "the contrast this test relies on does not hold on this host")

    def test_d_a_deliberately_hanging_control_is_actually_killed(self):
        """The one non-negotiable methodology claim: the timeout is
        enforced by THIS process against a child that cannot intercept it.
        A script that blocks in an uninterruptible-from-Python way (reading
        an unconnected FIFO) is killed within the requested wall clock, and
        reports TIMEOUT rather than ever printing its 'done' line."""
        root = af.make_short_root()
        try:
            fifo = root / "hang-forever"
            os.mkfifo(fifo)
            script = (
                f"open({str(fifo)!r}, 'rb').read()\n"
                "print('SHOULD-NEVER-PRINT')\n")
            import time
            t0 = time.monotonic()
            result = run_script(script, timeout_s=0.5)
            elapsed = time.monotonic() - t0
        finally:
            af.teardown_root(root)
        assert result["classification"] == "TIMEOUT"
        # Killed near the requested bound, not left to run to completion
        # (which would be "forever" -- this assertion is what proves the
        # kill, not merely the eventual timeout accounting).
        assert elapsed < 3.0, (
            f"the child was not promptly killed: parent blocked {elapsed}s "
            "past a 0.5s timeout")
