"""Static AST audit: every raw filesystem-presence call in the canonical
archive modules, classified, against an explicit allowlist.

The milestone's requirement is specific: no production module under
`app/realtime/` may touch a canonical evidence path except through the
shared total primitives `segment._presence` / `segment.containment_reason`
(and, transitively, `segment.assert_contained`). This module does not (and
cannot, from pure AST) prove that requirement holds -- proving it would
require alias/points-to analysis this codebase does not have. What it CAN
do, and does, is:

1. Enumerate every call to a filesystem-presence/read primitive
   (`Path.glob`, `Path.exists`, `Path.is_file`, `Path.is_dir`,
   `Path.is_symlink`, `Path.resolve`, `Path.read_bytes`, `Path.iterdir`,
   `Path.rglob`, `open`, `os.open`, `os.scandir`, `os.stat`, `os.lstat`,
   `os.readlink`, `gzip.open`) in the modules that own canonical archive
   evidence paths, using `ast`, not `grep` -- so a call split across lines,
   inside a comprehension, or shadowed by a rebound name is still found.
2. Attribute each occurrence to (module, enclosing function/class, call).
3. Compare that set against an explicit, reviewed ALLOWLIST recorded here.

A NEW occurrence not on the allowlist FAILS the test. This is a tripwire
for regressions, not a proof of the invariant -- see the harness report for
which of the allowlisted entries are themselves already-known defects
(e.g. `archive_head._read_json`'s `path.is_symlink()` gates a `read_bytes()`
that can hang forever on a FIFO; it is allowlisted because it EXISTS today,
not because it is correct).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

AUDITED_MODULES = (
    "app/realtime/archive.py",
    "app/realtime/archive_head.py",
    "app/realtime/segment.py",
    "app/realtime/legacy_import.py",
)

PATH_METHOD_NAMES = {
    "glob", "exists", "is_file", "is_dir", "is_symlink", "resolve",
    "read_bytes", "iterdir", "rglob",
}
OS_FUNC_NAMES = {"open", "scandir", "stat", "lstat", "readlink"}
GZIP_FUNC_NAMES = {"open"}
# Not in the milestone's explicit sweep list, but directly implicated in
# finding #5 (`read_genesis` treats EACCES the same as ENOENT because
# `os.path.lexists` swallows OSError internally) -- tracked anyway so the
# audit's own findings corroborate that defect rather than being silent
# about the single most-used existence check in this codebase.
OS_PATH_FUNC_NAMES = {"lexists", "exists"}


class _Finding:
    __slots__ = ("module", "qualname", "lineno", "call", "text")

    def __init__(self, module, qualname, lineno, call, text):
        self.module = module
        self.qualname = qualname
        self.lineno = lineno
        self.call = call
        self.text = text

    def key(self):
        return (self.module, self.qualname, self.call)

    def __repr__(self):
        return (f"{self.module}:{self.lineno} [{self.qualname}] "
               f"{self.call}  -- {self.text.strip()}")


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str, source_lines: list):
        self.module = module
        self.source_lines = source_lines
        self.stack: list = ["<module>"]
        self.findings: list = []

    def _qualname(self) -> str:
        return "<module>" if len(self.stack) == 1 else ".".join(self.stack[1:])

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def _text(self, node) -> str:
        try:
            return self.source_lines[node.lineno - 1]
        except IndexError:
            return ""

    def visit_Call(self, node):
        func = node.func
        call_desc = None
        if isinstance(func, ast.Attribute):
            if (isinstance(func.value, ast.Attribute)
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "os"
                    and func.value.attr == "path"
                    and func.attr in OS_PATH_FUNC_NAMES):
                call_desc = f"os.path.{func.attr}(...)"
            elif func.attr in PATH_METHOD_NAMES:
                call_desc = f"<expr>.{func.attr}(...)"
            elif (isinstance(func.value, ast.Name)
                  and func.value.id == "os" and func.attr in OS_FUNC_NAMES):
                call_desc = f"os.{func.attr}(...)"
            elif (isinstance(func.value, ast.Name)
                  and func.value.id == "gzip" and func.attr in GZIP_FUNC_NAMES):
                call_desc = f"gzip.{func.attr}(...)"
            elif (isinstance(func.value, ast.Name)
                  and func.value.id in ("_gz",) and func.attr == "open"):
                call_desc = "gzip.open(...)"       # `import gzip as _gz`
        elif isinstance(func, ast.Name) and func.id == "open":
            call_desc = "open(...)"

        if call_desc is not None:
            self.findings.append(_Finding(
                self.module, self._qualname(), node.lineno, call_desc,
                self._text(node)))
        self.generic_visit(node)


def scan_module(relpath: str) -> list:
    path = REPO_ROOT / relpath
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    visitor = _Visitor(relpath, source.splitlines())
    visitor.visit(tree)
    return visitor.findings


def scan_all() -> list:
    out = []
    for m in AUDITED_MODULES:
        out.extend(scan_module(m))
    return out


# --- the allowlist ------------------------------------------------------------
#
# (module, enclosing qualname, call description). Generated from the audited
# state of commit 1203663 (worktree/kalshi-archive-replay-integrity) and
# reviewed by hand against the source above. Anything NOT in this set is a
# NEW evidence-path access this harness has not seen before, and the test
# using it fails until a human adds it here deliberately.
#
# Three entries are marked KNOWN-DEFECT inline: they are on the allowlist
# because they exist today, not because they are correct. Fixing them
# should NOT require touching this file (removing a bad call site never
# needs allowlist surgery); adding a new *raw* call site anywhere else will.
ALLOWLIST = {
    # --- archive.py -------------------------------------------------------
    ("app/realtime/archive.py", "EventArchive._candidate_segment_ids", "<expr>.exists(...)"),  # KNOWN-DEFECT #5
    ("app/realtime/archive.py", "EventArchive._committed_segment_dirs", "<expr>.is_dir(...)"),
    ("app/realtime/archive.py", "EventArchive._committed_segment_dirs", "<expr>.is_symlink(...)"),
    ("app/realtime/archive.py", "EventArchive._next_segment_id", "<expr>.exists(...)"),         # KNOWN-DEFECT #5
    ("app/realtime/archive.py", "EventArchive._segment_dirs", "<expr>.exists(...)"),
    ("app/realtime/archive.py", "EventArchive._segment_dirs", "<expr>.glob(...)"),
    ("app/realtime/archive.py", "EventArchive._segment_dirs", "<expr>.is_dir(...)"),
    ("app/realtime/archive.py", "_undecodable_tail_records", "<expr>.read_bytes(...)"),  # KNOWN-DEFECT #4
    ("app/realtime/archive.py", "_undecodable_tail_records", "gzip.open(...)"),

    # --- archive_head.py ---------------------------------------------------
    ("app/realtime/archive_head.py", "_commit_locked", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "_fsync_directory", "os.open(...)"),
    ("app/realtime/archive_head.py", "_manifest_present", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "_publish_create_once", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "_publish_generation", "<expr>.read_bytes(...)"),
    ("app/realtime/archive_head.py", "_publish_replace", "<expr>.is_symlink(...)"),
    # KNOWN-DEFECT #3: `_read_json` gates ONLY on `is_symlink()`, not on the
    # `_presence` primitive, so `read_bytes()` on a FIFO at any head artifact
    # hangs forever. Allowlisted because it exists today, not because it is
    # total -- the fix is routing this through `_presence` before it opens
    # anything, which is out of scope for this harness (test-only change).
    ("app/realtime/archive_head.py", "_read_json", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "_read_json", "<expr>.read_bytes(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "<expr>.read_bytes(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "os.open(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "archive_lock", "os.open(...)"),
    # KNOWN-DEFECT #5c: `os.path.lexists` swallows EVERY `OSError`,
    # including `EACCES` -- so a genesis an operator cannot READ (permission
    # revoked, not deleted) is misreported by `read_genesis` as
    # `ArchiveNotInitializedError` / `head_state() == "NOT_INITIALIZED"`,
    # which is precisely the "missing genesis is never a new archive"
    # inference this module's docstring says must never be drawn from a
    # false signal.
    ("app/realtime/archive_head.py", "initialize_archive", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_dir(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_file(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.iterdir(...)"),
    ("app/realtime/archive_head.py", "read_current_head", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "read_generation", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "read_genesis", "os.path.lexists(...)"),   # KNOWN-DEFECT #5c
    ("app/realtime/archive_head.py", "recover_current_head", "os.path.lexists(...)"),

    # --- legacy_import.py: reads a NON-canonical, read-only external source.
    # Out of the canonical-evidence-path invariant by design (the module's
    # own docstring: "the legacy source is opened read-only ... never
    # written, moved or deleted"), listed here so a NEW access inside this
    # file is still visible to a reviewer even though it is not gated by
    # `_presence`.
    ("app/realtime/legacy_import.py", "_file_digest", "open(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_dir(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_file(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_symlink(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.rglob(...)"),
    ("app/realtime/legacy_import.py", "_read_legacy_records", "<expr>.read_bytes(...)"),
    ("app/realtime/legacy_import.py", "migrate_legacy_archive", "<expr>.resolve(...)"),
    ("app/realtime/legacy_import.py", "migrate_legacy_archive", "os.path.lexists(...)"),

    # --- segment.py: the primitives themselves -----------------------------
    ("app/realtime/segment.py", "assert_contained", "<expr>.exists(...)"),
    ("app/realtime/segment.py", "assert_contained", "<expr>.is_dir(...)"),
    ("app/realtime/segment.py", "assert_contained", "<expr>.is_symlink(...)"),
    ("app/realtime/segment.py", "assert_contained", "<expr>.resolve(...)"),
    ("app/realtime/segment.py", "_presence", "os.lstat(...)"),
    ("app/realtime/segment.py", "_fsync_directory", "os.open(...)"),

    # --- segment.py: writer-side (owns its OWN segment; the flock in
    # `_acquire_ownership` is the single-writer guarantee this whole area
    # depends on, so a symlink/FIFO/permission probe here is a probe against
    # the writer, which this harness's matrix does not target -- append()'s
    # candidate-id scan is covered above under archive.py).
    ("app/realtime/segment.py", "SegmentWriter.__init__", "<expr>.exists(...)"),
    ("app/realtime/segment.py", "SegmentWriter.__init__", "<expr>.resolve(...)"),
    ("app/realtime/segment.py", "SegmentWriter._acquire_ownership", "os.open(...)"),
    ("app/realtime/segment.py", "SegmentWriter._close_stages", "os.open(...)"),
    ("app/realtime/segment.py", "SegmentWriter._open_events", "gzip.open(...)"),
    ("app/realtime/segment.py", "SegmentWriter._open_events", "os.open(...)"),
    ("app/realtime/segment.py", "SegmentWriter._quarantine_abandoned_events", "<expr>.exists(...)"),
    ("app/realtime/segment.py", "SegmentWriter.read_manifest", "<expr>.read_bytes(...)"),
    ("app/realtime/segment.py", "file_sha256", "open(...)"),
    ("app/realtime/segment.py", "publish_manifest", "<expr>.read_bytes(...)"),
    ("app/realtime/segment.py", "publish_manifest", "os.open(...)"),
    ("app/realtime/segment.py", "publish_manifest", "os.path.lexists(...)"),

    # --- segment.py: read_segment_records -- the sibling `_undecodable_tail_
    # records` in archive.py was supposed to mirror this `is_file()` guard
    # and does not (KNOWN-DEFECT #4).
    ("app/realtime/segment.py", "read_segment_records", "<expr>.is_file(...)"),
    ("app/realtime/segment.py", "read_segment_records", "<expr>.read_bytes(...)"),

    # --- segment.py: verify_segment -- reads manifest bytes directly once
    # presence is already proven total by its own `_presence` calls.
    ("app/realtime/segment.py", "verify_segment", "<expr>.read_bytes(...)"),

    # --- segment.py: KNOWN-DEFECT -- the outer enumeration glob calls that
    # swallow EACCES silently (finding #2) and skip symlinked directories
    # outright (finding #1). Allowlisted because they exist; NOT because
    # they are total.
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.glob(...)"),      # KNOWN-DEFECT #2 (line 2048)
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.is_dir(...)"),    # KNOWN-DEFECT #1 (twin of :2185)
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.is_symlink(...)"),
    ("app/realtime/segment.py", "_abandoned_residue", "os.scandir(...)"),
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.exists(...)"),
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.glob(...)"),   # KNOWN-DEFECT #2 (line 2179)
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.is_dir(...)"),
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.is_symlink(...)"),  # KNOWN-DEFECT #1 (line 2185)
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.read_bytes(...)"),
}


def diff_against_allowlist(findings: list) -> list:
    """Findings whose key is NOT in ALLOWLIST. Empty means the audit is clean."""
    return [f for f in findings if f.key() not in ALLOWLIST]
