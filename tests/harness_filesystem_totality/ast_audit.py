"""Static AST audit: every raw filesystem-presence call in the canonical
archive modules, classified, against an explicit allowlist.

KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1: the milestone's requirement is now
enforced by construction, not merely by convention. `app/realtime/
evidence_fs.py` is the ONE evidence-filesystem abstraction every canonical
archive module (`archive.py`, `archive_head.py`, `segment.py`,
`legacy_import.py`) routes through for presence, containment, bounded
reads, and safe enumeration -- and it is now itself AUDITED, alongside
those four, specifically so that a NEW raw filesystem call added to the
one module that is supposed to be the sole home for such calls is exactly
as visible to this sweep as a new raw call added anywhere else. Before A1,
`segment._presence` and `segment.containment_reason` existed but NO
production reader in `archive_head.py` or `archive.py` ever called either
one -- that gap, not a missing primitive, was the root cause the eight
prior remediation rounds kept re-discovering one instance at a time.

This module does not (and cannot, from pure AST) prove the routing
invariant holds -- proving it would require alias/points-to analysis this
codebase does not have. What it CAN do, and does, is:

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
which of the allowlisted entries are write-side (segment ownership,
durable publication) and therefore legitimately out of the READ-path
totality this milestone targets, versus which are `evidence_fs`'s own
foundational primitives.
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
    # The abstraction itself. Every raw filesystem primitive the other four
    # modules used to call directly now lives HERE -- so a new raw call
    # anywhere else in those four modules is a regression back toward the
    # pre-A1 pattern, and a new raw call added IN HERE, outside the six
    # reviewed primitives below, is exactly as much a review event as either.
    "app/realtime/evidence_fs.py",
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
# (module, enclosing qualname, call description). Re-generated from the
# audited state after KALSHI-ARCHIVE-CORE-REMEDIATION-002 A1 and reviewed by
# hand against the source above. Anything NOT in this set is a NEW
# evidence-path access this harness has not seen before, and the test using
# it fails until a human adds it here deliberately.
#
# Every entry that used to carry a `KNOWN-DEFECT` marker is GONE from this
# table, not merely un-marked: the call sites that produced defects #1-#6
# were themselves removed (their raw `Path.glob`/`.read_bytes()`/
# `os.path.lexists` calls no longer exist in these four files) rather than
# patched in place, because the fix was to route them through
# `evidence_fs`'s shared primitives -- which is why `evidence_fs.py` is now
# its own audited module with its own six-entry section below, rather than
# these calls simply vanishing from the sweep with nothing to show for it.
ALLOWLIST = {
    # --- archive.py -------------------------------------------------------
    ("app/realtime/archive.py", "EventArchive._committed_segment_dirs", "<expr>.is_dir(...)"),
    ("app/realtime/archive.py", "EventArchive._committed_segment_dirs", "<expr>.is_symlink(...)"),
    ("app/realtime/archive.py", "EventArchive._segment_dirs", "<expr>.is_dir(...)"),

    # --- archive_head.py ---------------------------------------------------
    ("app/realtime/archive_head.py", "_commit_locked", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "_fsync_directory", "os.open(...)"),
    ("app/realtime/archive_head.py", "_manifest_present", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "_publish_create_once", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "_publish_generation", "<expr>.read_bytes(...)"),
    ("app/realtime/archive_head.py", "_publish_replace", "<expr>.is_symlink(...)"),
    # `_read_json` now gates on `evidence_fs.presence` (total) before ever
    # checking `is_symlink()`; this one remaining call is the deliberate
    # containment refusal -- head artifacts must never themselves BE a
    # symlink, independent of what they would resolve to -- not a guard in
    # front of an unbounded `read_bytes()` any more (that read is now
    # `evidence_fs.bounded_read`, which does not appear as a raw call here
    # because it lives in `evidence_fs.py`).
    ("app/realtime/archive_head.py", "_read_json", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "<expr>.read_bytes(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "os.open(...)"),
    ("app/realtime/archive_head.py", "_stage_bytes", "os.path.lexists(...)"),
    # Write-side lock acquisition, not an evidence READ -- see the function's
    # own comment: a raw `PermissionError` from this `os.open` is now caught
    # and re-raised as `ArchiveHeadError` (defect #5b), which changes the
    # exception boundary, not the call site, so no allowlist entry moved.
    ("app/realtime/archive_head.py", "archive_lock", "os.open(...)"),
    ("app/realtime/archive_head.py", "initialize_archive", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_dir(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_file(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.is_symlink(...)"),
    ("app/realtime/archive_head.py", "present_generations", "<expr>.iterdir(...)"),
    ("app/realtime/archive_head.py", "read_current_head", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "read_generation", "os.path.lexists(...)"),
    ("app/realtime/archive_head.py", "recover_current_head", "os.path.lexists(...)"),
    # `read_genesis`'s own `os.path.lexists(path)` existence check is GONE
    # (defect #5c): it now calls `evidence_fs.presence`, which distinguishes
    # "missing" from "cannot be examined" -- the property `os.path.lexists`
    # cannot carry, since it swallows every `OSError` including `EACCES`.

    # --- legacy_import.py: reads a NON-canonical, read-only external source.
    # Out of the canonical-evidence-path invariant by design (the module's
    # own docstring: "the legacy source is opened read-only ... never
    # written, moved or deleted"), listed here so a NEW access inside this
    # file is still visible to a reviewer even though it is not gated by
    # `evidence_fs`.
    ("app/realtime/legacy_import.py", "_file_digest", "open(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_dir(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_file(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.is_symlink(...)"),
    ("app/realtime/legacy_import.py", "_legacy_files", "<expr>.rglob(...)"),
    ("app/realtime/legacy_import.py", "_read_legacy_records", "<expr>.read_bytes(...)"),
    ("app/realtime/legacy_import.py", "migrate_legacy_archive", "<expr>.resolve(...)"),
    ("app/realtime/legacy_import.py", "migrate_legacy_archive", "os.path.lexists(...)"),

    # --- segment.py: `assert_contained`/`containment_reason`/`_presence` are
    # now thin delegations to `evidence_fs` (see that module's section
    # below) and make NO raw filesystem call of their own any more -- there
    # is deliberately nothing left to allowlist for them here.
    ("app/realtime/segment.py", "_fsync_directory", "os.open(...)"),

    # --- segment.py: writer-side (owns its OWN segment; the flock in
    # `_acquire_ownership` is the single-writer guarantee this whole area
    # depends on, so a symlink/FIFO/permission probe here is a probe against
    # the writer, which this harness's matrix does not target -- append()'s
    # candidate-id scan is covered above under archive.py, and now routes
    # through `evidence_fs.presence`).
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

    # --- segment.py: read_segment_records -- unchanged; already correctly
    # checked `is_file()` before reading (the property `_undecodable_tail_
    # records`'s sibling call in `archive.py` was missing -- fixed by
    # routing THAT call through `evidence_fs.bounded_read` instead, so it no
    # longer appears as a raw call in `archive.py` at all).
    ("app/realtime/segment.py", "read_segment_records", "<expr>.is_file(...)"),
    ("app/realtime/segment.py", "read_segment_records", "<expr>.read_bytes(...)"),

    # --- segment.py: the outer enumeration loops now route through
    # `evidence_fs.safe_enumerate` (honest about EACCES; fixes finding #2)
    # and record -- rather than silently `continue` past -- a symlinked
    # segment directory (fixes finding #1). The `is_symlink()`/`is_dir()`
    # calls that remain are the DISCOVERY-loop classification of each
    # enumerated child, not the enumeration itself.
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.is_symlink(...)"),
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.is_dir(...)"),
    ("app/realtime/segment.py", "_abandoned_residue", "os.scandir(...)"),  # filename-prefix filter, not a glob pattern -- see its own docstring
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.is_symlink(...)"),
    ("app/realtime/segment.py", "_verify_archive_inner", "<expr>.is_dir(...)"),

    # --- evidence_fs.py: the abstraction's own foundational primitives. ---
    # These SIX are, deliberately, the only raw filesystem-presence/read
    # calls anywhere in the four canonical archive modules' evidence path.
    # `presence` uses `os.lstat` (never follows the final component, so it
    # cannot itself block on a FIFO by opening it); `assert_contained` walks
    # the path component-by-component with `is_symlink`/`exists`/`is_dir`,
    # matching a candidate spelling of the target against the root with
    # `resolve` (twice: once for the root, once for a target that itself
    # exists); `safe_enumerate` uses `os.scandir` directly because it is
    # honest about `EACCES`, unlike `Path.glob`; `bounded_read` uses
    # `os.stat` (follows symlinks, to see what a chain ultimately resolves
    # to) then a guarded `open` -- never called until the `stat` has already
    # proven the target is a regular file under the size bound.
    ("app/realtime/evidence_fs.py", "presence", "os.lstat(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.resolve(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.exists(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.is_symlink(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.is_dir(...)"),
    ("app/realtime/evidence_fs.py", "safe_enumerate", "os.scandir(...)"),
    ("app/realtime/evidence_fs.py", "is_regular_file", "os.stat(...)"),
    ("app/realtime/evidence_fs.py", "bounded_read", "os.stat(...)"),
    ("app/realtime/evidence_fs.py", "bounded_read", "open(...)"),
}


def diff_against_allowlist(findings: list) -> list:
    """Findings whose key is NOT in ALLOWLIST. Empty means the audit is clean."""
    return [f for f in findings if f.key() not in ALLOWLIST]
