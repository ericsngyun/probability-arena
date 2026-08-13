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

KALSHI-ARCHIVE-VERIFICATION-META-001 (A2 + the prerequisite consolidation
before A9's mutation campaign): this module used to be the ONLY guard, and
a red-team corpus of 22 unsafe/8 safe shapes proved it bypassed 16 of 22 of
them -- `Path(p).open()`, `path.stat()`, an aliased `helper = open`, a
`getattr(path, "open")()` dynamic dispatch, `os.listdir`/`os.walk`/
`os.path.realpath`/`shutil.copyfile`/`io.open`/`gzip.GzipFile`, and an
IMPORT-aliased module (`import gzip as _gz` was special-cased; nothing
else was) were all structurally invisible to its vocabulary, not merely
missing from its allowlist. A second guard (`tests/meta_inventory/
ast_guard_v2.py`) was built to close those gaps as NEW infrastructure,
which produced exactly the failure mode this milestone's prerequisite
warns about: mutating ONE guard left the OTHER one green, so the mutation
campaign would have certified nothing. `ast_guard_v2.py` is gone; its
detections (wider attribute-name vocabulary, wider module/function
vocabulary, execution-order alias tracking for both builtin rebinding and
import aliasing, and `getattr(...)` dynamic-dispatch detection) are folded
in below, so `_Visitor` is now the ONE and ONLY static guard covering the
five audited modules. Seven call sites this widened vocabulary made newly
visible for the first time -- the five a prior line-based delta check
found (four `<expr>.stat(...)` sites, one `gzip.GzipFile(...)`) PLUS two
more a per-LINE dedup missed because they share a source line with an
already-allowlisted call (`SegmentWriter._quarantine_abandoned_events`'s
`.stat()` sharing a line with its already-allowlisted `.exists()`, and
`SegmentWriter._open_events`'s `os.fdopen(...)` nested inside its
already-allowlisted `gzip.open(...)` call) -- were hand-reviewed and are
recorded in the ALLOWLIST below with an explicit safety verdict for each.

This module does not (and cannot, from pure AST) prove the routing
invariant holds -- proving it would require alias/points-to analysis this
codebase does not have. What it CAN do, and does, is:

1. Enumerate every call to a filesystem-presence/read primitive
   (`Path.glob`, `Path.exists`, `Path.is_file`, `Path.is_dir`,
   `Path.is_symlink`, `Path.resolve`, `Path.read_bytes`, `Path.read_text`,
   `Path.iterdir`, `Path.rglob`, `Path.open`, `Path.stat`, `Path.lstat`,
   `open`, `os.open`, `os.scandir`, `os.stat`, `os.lstat`, `os.readlink`,
   `os.fdopen`, `os.listdir`, `os.walk`, `os.path.realpath`,
   `shutil.copyfile`, `io.open`, `gzip.open`, `gzip.GzipFile`), an ALIASED
   builtin (`helper = open; helper(...)`), an ALIASED import of any of the
   above (`import gzip as _gz`, `from os import path as p`), and a
   `getattr(obj, "<unsafe-name>")(...)` dynamic dispatch -- in the modules
   that own canonical archive evidence paths, using `ast`, not `grep` -- so
   a call split across lines, inside a comprehension, or shadowed by a
   rebound name is still found.
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

# --- detection vocabulary ----------------------------------------------------
#
# Superset of the original four-set vocabulary (which alone bypassed 16/22
# red-team shapes) merged with everything `ast_guard_v2.py` added. Matched on
# the ATTRIBUTE/FUNCTION NAME alone, deliberately not conditioned on the
# receiver's inferred type -- a subclassed Path-like object or an aliased
# import changes the receiver's spelling, not the method name, and matching
# the name is exactly what survives that (same over-inclusive tradeoff
# `ast_guard_v2.py` documented: this WILL flag a non-filesystem `.open()` --
# a socket, a zipfile -- as a false positive on a real production module;
# that is a stated tradeoff, not an oversight).
PATH_METHOD_NAMES = {
    "glob", "exists", "is_file", "is_dir", "is_symlink", "resolve",
    "read_bytes", "read_text", "iterdir", "rglob", "open", "stat", "lstat",
}
OS_FUNC_NAMES = {"open", "scandir", "stat", "lstat", "readlink", "fdopen",
                 "listdir", "walk"}
GZIP_FUNC_NAMES = {"open", "GzipFile"}
SHUTIL_FUNC_NAMES = {"copyfile"}
IO_FUNC_NAMES = {"open"}
# Not in the milestone's explicit sweep list, but directly implicated in
# finding #5 (`read_genesis` treats EACCES the same as ENOENT because
# `os.path.lexists` swallows OSError internally) -- tracked anyway so the
# audit's own findings corroborate that defect rather than being silent
# about the single most-used existence check in this codebase.
OS_PATH_FUNC_NAMES = {"lexists", "exists", "realpath"}
# Builtins whose NAME, if rebound to a plain local (`helper = open`) or
# imported directly (`from os import fdopen as helper`), still makes a
# later `helper(...)` call site a filesystem primitive.
UNSAFE_BUILTIN_NAMES = {"open"}
# `getattr(obj, "<name>")(...)` -- the SAME set as the path-method names,
# since that is the dynamic-dispatch shape the red-team corpus exercises.
GETATTR_UNSAFE_ARGS = PATH_METHOD_NAMES | {"read_text"}
# (module dotted-path -> allowed attr names), used for import-alias
# resolution: `import gzip as _gz; _gz.open(...)`, `from os import path as
# p; p.realpath(...)`, `import shutil as sh; sh.copyfile(...)`.
_MODULE_FUNC_NAMES = {
    "os": OS_FUNC_NAMES, "os.path": OS_PATH_FUNC_NAMES,
    "gzip": GZIP_FUNC_NAMES, "shutil": SHUTIL_FUNC_NAMES, "io": IO_FUNC_NAMES,
}


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
    """The one and only static guard. Tracks import aliases and simple
    `name = <unsafe-callable>` local rebindings, in EXECUTION ORDER (a
    top-to-bottom scan, not general-purpose points-to analysis -- sufficient
    for this red-team corpus's deliberately linear snippets and for this
    codebase's module-level import style), so an aliased builtin or an
    aliased import is resolved through to the same finding an unaliased
    spelling would produce.
    """

    def __init__(self, module: str, source_lines: list):
        self.module = module
        self.source_lines = source_lines
        self.stack: list = ["<module>"]
        self.findings: list = []
        # name -> ("builtin", builtin_name) | ("module_attr", dotted_module, attr)
        self.aliases: dict = {}
        # bound name -> dotted module path, from `import X as name` / `import X`
        self.module_aliases: dict = {}

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

    def _flag(self, node, call_desc: str) -> None:
        self.findings.append(_Finding(
            self.module, self._qualname(), node.lineno, call_desc,
            self._text(node)))

    # --- import / alias tracking (ordered, top-to-bottom) ---------------------

    def visit_Import(self, node):
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.module_aliases[bound] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module is not None:
            for alias in node.names:
                bound = alias.asname or alias.name
                # `from pathlib import Path as P` -- a NAME import of a
                # class, not a module; deliberately NOT recorded as a module
                # alias (calling `.open()` on a `P(...)` instance is caught
                # by the attribute-name match in visit_Call, not by this
                # path).
                if node.module in _MODULE_FUNC_NAMES and (
                        alias.name in _MODULE_FUNC_NAMES[node.module]):
                    self.aliases[bound] = ("module_attr", node.module, alias.name)
                elif node.module == "os" and alias.name == "path":
                    self.module_aliases[bound] = "os.path"
        self.generic_visit(node)

    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value = node.value
            if isinstance(value, ast.Name):
                if value.id in UNSAFE_BUILTIN_NAMES:
                    self.aliases[target_name] = ("builtin", value.id)
                elif value.id in self.aliases:
                    self.aliases[target_name] = self.aliases[value.id]
        self.generic_visit(node)

    def _resolve_module_attr_call(self, func: ast.Attribute):
        """For `expr.attr(...)`, return (dotted_module, attr) if `expr`
        resolves (through import-alias tracking) to a known module."""
        value = func.value
        if isinstance(value, ast.Name):
            base = value.id
            if base in self.module_aliases:
                return (self.module_aliases[base], func.attr)
            if base in ("os", "gzip", "shutil", "io"):
                return (base, func.attr)
            return None
        if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
            outer_base = value.value.id
            dotted = self.module_aliases.get(outer_base, outer_base)
            return (f"{dotted}.{value.attr}", func.attr)
        return None

    # --- the actual sweep -------------------------------------------------

    def visit_Call(self, node):
        func = node.func

        if isinstance(func, ast.Attribute):
            # Module-qualified resolution (`os.open`, `os.path.lexists`,
            # `gzip.open`/`GzipFile`, an ALIASED `import gzip as _gz`, ...)
            # is tried FIRST and takes priority over the generic
            # `<expr>.method(...)` match below: `open`/`stat`/`lstat` are
            # members of BOTH `PATH_METHOD_NAMES` and `OS_FUNC_NAMES`, so
            # checking the generic bucket first would mislabel `os.open(...)`
            # as `<expr>.open(...)` and break every existing allowlist entry
            # spelled the `os.`-qualified way.
            resolved = self._resolve_module_attr_call(func)
            if (resolved is not None and resolved[0] in _MODULE_FUNC_NAMES
                    and resolved[1] in _MODULE_FUNC_NAMES[resolved[0]]):
                dotted, attr = resolved
                self._flag(node, f"{dotted}.{attr}(...)")
            elif func.attr in PATH_METHOD_NAMES:
                self._flag(node, f"<expr>.{func.attr}(...)")

        elif isinstance(func, ast.Name):
            if func.id == "open":
                self._flag(node, "open(...)")
            elif func.id in self.aliases:
                kind = self.aliases[func.id]
                if kind[0] == "builtin":
                    self._flag(node, f"open(...)  # via alias {func.id!r}")
                else:
                    self._flag(node, f"{kind[1]}.{kind[2]}(...)  # via alias "
                                     f"{func.id!r}")
            elif func.id == "getattr" and len(node.args) >= 2:
                second = node.args[1]
                if (isinstance(second, ast.Constant)
                        and isinstance(second.value, str)
                        and second.value in GETATTR_UNSAFE_ARGS):
                    self._flag(node, f"getattr(..., {second.value!r})(...)")

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


def scan_source(source: str, *, module: str = "<corpus>") -> list:
    """Scan a source STRING rather than a repo-relative file -- used by the
    red-team corpus (`tests/meta_inventory/red_team_corpus.py`), which is
    deliberately guard-agnostic: it exercises whichever module this
    function lives in, so folding two guards into one required no change to
    the corpus itself.
    """
    tree = ast.parse(source, filename=module)
    visitor = _Visitor(module, source.splitlines())
    visitor.visit(tree)
    return visitor.findings


def scan_file(path) -> list:
    text = Path(path).read_text()
    tree = ast.parse(text, filename=str(path))
    visitor = _Visitor(str(path), text.splitlines())
    visitor.visit(tree)
    return visitor.findings


# --- the allowlist ------------------------------------------------------------
#
# (module, enclosing qualname, call description). Re-generated from the
# CONSOLIDATED (single-guard, widened-vocabulary) scan and reviewed by hand
# against the source above. Anything NOT in this set is a NEW evidence-path
# access this harness has not seen before, and the test using it fails
# until a human adds it here deliberately.
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
    # SAFE, newly visible under the widened `OS_FUNC_NAMES` (added "fdopen"):
    # wraps the FD `os.open(...)` on the line immediately above already
    # opened (with `O_NOFOLLOW`, against the writer's OWN events path) into
    # a Python file object -- `os.fdopen` never itself makes a filesystem
    # `open()` syscall; it operates on an already-validated file descriptor,
    # not a path. Same writer-owned threat model as the `os.open` call it
    # wraps, two names for what is structurally one guarded open.
    ("app/realtime/segment.py", "SegmentWriter._open_events", "os.fdopen(...)"),
    ("app/realtime/segment.py", "SegmentWriter._quarantine_abandoned_events", "<expr>.exists(...)"),
    # SAFE, newly visible: the companion `.stat()` on the SAME line/SAME
    # try/except OSError block as the `.exists()` check immediately above --
    # both against the writer's own `self.events_path`, both refuse a
    # symlink two lines earlier (`os.path.islink`), and both fail closed
    # (`except OSError: return`) rather than propagating. Writer-owned, not
    # a reader being handed an untrusted path.
    ("app/realtime/segment.py", "SegmentWriter._quarantine_abandoned_events", "<expr>.stat(...)"),
    ("app/realtime/segment.py", "SegmentWriter.read_manifest", "<expr>.read_bytes(...)"),
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect A, FIXED: `file_sha256`'s
    # raw `open(...)` (the named finding, `verify_segment -> file_sha256 ->
    # open()`) is gone -- `file_sha256` now delegates entirely to
    # `evidence_fs.sha256_bounded`, so there is nothing left to allowlist
    # for it here. Left as a comment, not a silently-dropped line, so the
    # history of what used to be here stays visible in the diff.
    ("app/realtime/segment.py", "publish_manifest", "<expr>.read_bytes(...)"),
    ("app/realtime/segment.py", "publish_manifest", "os.open(...)"),
    ("app/realtime/segment.py", "publish_manifest", "os.path.lexists(...)"),

    # --- segment.py: read_segment_records -- KALSHI-ARCHIVE-CORE-
    # REMEDIATION-003 defect A, FIXED: the `<expr>.is_file()` (check) then
    # `<expr>.read_bytes()` (use) shape was a TOCTOU on the same
    # symlink-to-FIFO/regular-file swap `verify_segment`'s containment block
    # exists to refuse, with no size bound at all. Both calls are gone --
    # `read_segment_records` now routes through `evidence_fs.bounded_read`
    # (the fd-based, race-free, size-bounded primitive), matching the fix
    # already applied to `archive.py`'s sibling call noted below.

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
    # These are, deliberately, the only raw filesystem-presence/read
    # calls anywhere in the four canonical archive modules' evidence path.
    # `presence` uses `os.lstat` (never follows the final component, so it
    # cannot itself block on a FIFO by opening it); `assert_contained` walks
    # the path component-by-component with `is_symlink`/`exists`/`is_dir`,
    # matching a candidate spelling of the target against the root with
    # `resolve` (twice: once for the root, once for a target that itself
    # exists); `safe_enumerate` uses `os.scandir` directly because it is
    # honest about `EACCES`, unlike `Path.glob`.
    #
    # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect A, FIXED: `bounded_read`
    # used to `os.stat(path)` (check) then a separately-guarded `open(path)`
    # (use) -- a check-then-use TOCTOU, and `open()`/`Path.open()` follow a
    # symlink at the final component regardless of what the earlier `stat`
    # proved. `bounded_read`, `sha256_bounded` and `stat_and_sha256_bounded`
    # now all route through `_open_verified_fd`: ONE `os.open(...,
    # O_NOFOLLOW)` acquires the fd atomically (refusing a final-component
    # symlink as part of the same syscall), then `os.fstat` proves the type
    # ON THE FD -- not the path -- so nothing that happens to the name after
    # the open can change what gets read. This is the ONE new raw-open call
    # site in the whole evidence-path -- deliberately, since it is the
    # reviewed primitive every canonical reader now goes through instead of
    # calling `open()` itself.
    ("app/realtime/evidence_fs.py", "presence", "os.lstat(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.resolve(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.exists(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.is_symlink(...)"),
    ("app/realtime/evidence_fs.py", "assert_contained", "<expr>.is_dir(...)"),
    ("app/realtime/evidence_fs.py", "safe_enumerate", "os.scandir(...)"),
    ("app/realtime/evidence_fs.py", "is_regular_file", "os.stat(...)"),
    ("app/realtime/evidence_fs.py", "_open_verified_fd", "os.open(...)"),

    # --- NEWLY VISIBLE, KALSHI-ARCHIVE-VERIFICATION-META-001 -- the five
    # call sites the ORIGINAL (pre-consolidation) scanner's narrower
    # vocabulary never even visited (no PATH_METHOD_NAMES entry for "stat",
    # no GZIP_FUNC_NAMES entry for "GzipFile"). Each was hand-reviewed
    # against the containment/regular-file invariant this allowlist exists
    # to enforce; the verdict for each is recorded here, not merely the
    # allowlisting decision, per this milestone's explicit instruction not
    # to blanket-allowlist to make the suite green.
    #
    # SAFE -- writer-owned segment (`self.events_path`/`self.dir` are the
    # WRITER's own segment, created and exclusively locked by THIS writer
    # in `_acquire_ownership`/`_open_events`, already on this allowlist
    # above) -- a size check against a file the writer itself opened is not
    # a reader being handed an untrusted path; it is the same threat model
    # already accepted for `SegmentWriter.__init__`'s `<expr>.exists(...)`
    # and `_close_stages`'s `os.open(...)` two lines above this same stat.
    ("app/realtime/segment.py", "SegmentWriter.rotation_due", "<expr>.stat(...)"),
    ("app/realtime/segment.py", "SegmentWriter._close_stages", "<expr>.stat(...)"),

    # SAFE -- diagnostic quarantine reporting, size-only, already downstream
    # of the SAME per-file containment checks this function's other two
    # allowlisted calls perform (`d.is_symlink()`/`d.is_dir()` on the PARENT
    # directory, then `os.scandir(d)` with a filename-PREFIX filter, both
    # allowlisted above). `f` is a `Path` built directly from an
    # `os.scandir(d)` DirEntry's own `.path`, not a caller-supplied or
    # glob-resolved path; `.stat()` here reports a BYTE COUNT for an
    # operator's residue report, wrapped in `except OSError`, and is never
    # followed by an `open()` of `f`'s content. If `f` itself is a symlink
    # to something outside the root, the worst this call discloses is that
    # target's SIZE (matching the severity already accepted for the
    # `is_symlink()` DIRECTORY check two lines above, which reports "not
    # scanned" rather than silently skipping) -- no content is ever read.
    ("app/realtime/segment.py", "_abandoned_residue", "<expr>.stat(...)"),

    # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect A, FIXED (both halves of
    # this gap, together): `events_path.stat()` used to sit directly inside
    # `verify_segment`, immediately adjacent to the also-now-fixed
    # `file_sha256` raw `open()`, and ran with ZERO containment checking
    # whenever `root=None` was passed explicitly (the containment block used
    # to read `if root is not None: ...`, silently skipping itself for that
    # one falsy value). Both halves are closed: (1) `root=None` now RAISES
    # instead of skipping containment -- see `verify_segment`'s own
    # docstring/comment -- so the containment block always runs before any
    # read is reachable; (2) the raw `.stat()` + `file_sha256`'s raw `open()`
    # are both gone, replaced by ONE call to
    # `evidence_fs.stat_and_sha256_bounded`, which is a LOCAL function call
    # in `segment.py` (not a direct `<expr>.stat(...)`/`open(...)` in this
    # module at all) that itself routes through the fd-based
    # `_open_verified_fd` primitive allowlisted above. There is deliberately
    # nothing left to allowlist for `verify_segment` here.

    # SAFE -- operates on `io.BytesIO(data)`, an IN-MEMORY buffer already
    # produced by `bounded_read(path, ...)` two lines above, never on a
    # filesystem path itself. `bounded_read` has ALREADY proven the source
    # is a regular file under the size bound and read it fully into `data`
    # before this line runs; `gzip.GzipFile(fileobj=...)` never touches the
    # filesystem at all -- it is flagged by this guard's vocabulary purely
    # because the NAME `GzipFile` is deliberately over-inclusive (see the
    # module docstring), not because this call site can itself hang, read
    # unboundedly, or escape containment. This is exactly the shape the
    # module's own docstring explains: reading through `bounded_read` FIRST
    # means the syscall that could hang or read forever never happens
    # without the regular-file-and-size check in front of it.
    ("app/realtime/evidence_fs.py", "open_bounded_gzip", "gzip.GzipFile(...)"),
}


def diff_against_allowlist(findings: list) -> list:
    """Findings whose key is NOT in ALLOWLIST. Empty means the audit is clean."""
    return [f for f in findings if f.key() not in ALLOWLIST]
