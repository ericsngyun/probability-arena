"""A1 — production-API call graph, from every canonical archive entry point
down to every filesystem / canonical-encoding / state-mutation / hashing /
queue-accounting primitive it can reach.

WHY THIS EXISTS, and why it is not `tests/harness_filesystem_totality/
ast_audit.py` again: that guard enumerates raw filesystem calls PER MODULE,
with no notion of "entry point" and no traversal through a helper function.
It is a flat sweep, not a call graph. That is precisely why `verify_segment
-> file_sha256 -> open()` was invisible to a reviewer skimming its allowlist:
`file_sha256`'s `open(...)` IS on that allowlist (`("app/realtime/segment.py",
"file_sha256", "open(...)")`), but nothing in that harness ever states the
chain "an entry point a caller actually calls (`verify_segment`) reaches this
call two hops down, through a helper that does not itself look like a
filesystem primitive". A flat allowlist and a call graph answer different
questions; this module answers the second one.

Method, stated honestly: this is a best-effort STATIC call graph built from
`ast`, not a points-to or type analysis. It resolves:

  * bare name calls (`foo(...)`)              -> a function in the symbol
    table, resolved through `from X import foo` when `foo` is not defined in
    the current module;
  * `self.method(...)` / `cls.method(...)`    -> a method on the enclosing
    class;
  * `alias.attr(...)` where `alias` is a      -> that module's `attr`, when
    module-level import alias                    `alias` was bound by
                                                   `import X as alias` or
                                                   `from app... import X`
                                                   (module import, not name
                                                   import);
  * known primitive namespaces (`os`, `gzip`,
    `hashlib`, bare `open`)                    -> classified directly as a
                                                   PRIMITIVE LEAF, not
                                                   traversed further.

What it explicitly does NOT do: resolve calls through a variable holding a
function reference, resolve `getattr`-based dispatch, or follow into
`app/realtime/canonical.py`'s internals (its exported functions are treated
as CANONICAL ENCODING / HASHING primitive leaves rather than walked, since
they are the canonicalization contract every digest in this codebase is
defined against, not a filesystem access path). Both limitations are the
same class of gap the milestone exists to surface in the OLD guard, so they
are stated here rather than left implicit.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# --- modules this tool knows how to parse and traverse into -----------------

TRAVERSABLE_MODULES = (
    "app/realtime/archive.py",
    "app/realtime/archive_head.py",
    "app/realtime/segment.py",
    "app/realtime/legacy_import.py",
    "app/realtime/evidence_fs.py",
    "app/cli.py",
)

# --- canonical entry points: (module, qualname) ------------------------------
#
# "qualname" is `func` for a module-level function or `Class.method` for a
# method. This is the enumeration A1 requires: every public/canonical archive
# entry point. `app/cli.py`'s four `archive-*` operator commands are the
# complete set — `grep -n '"archive-' app/cli.py` finds no others.

ENTRY_POINTS = (
    ("app/realtime/archive_head.py", "initialize_archive"),
    ("app/realtime/archive_head.py", "read_genesis"),
    ("app/realtime/archive_head.py", "read_current_head"),
    ("app/realtime/archive_head.py", "read_generation"),
    ("app/realtime/archive_head.py", "load_authoritative_head"),
    ("app/realtime/archive_head.py", "commit_segment"),
    ("app/realtime/archive_head.py", "recover_current_head"),
    ("app/realtime/archive_head.py", "head_state"),
    ("app/realtime/archive_head.py", "present_generations"),
    ("app/realtime/segment.py", "verify_segment"),
    ("app/realtime/segment.py", "verify_archive"),
    ("app/realtime/segment.py", "read_segment_records"),
    ("app/realtime/segment.py", "file_sha256"),
    ("app/realtime/segment.py", "build_record"),
    ("app/realtime/segment.py", "verify_chain"),
    ("app/realtime/segment.py", "build_manifest"),
    ("app/realtime/segment.py", "verify_manifest_self_digest"),
    ("app/realtime/segment.py", "publish_manifest"),
    ("app/realtime/segment.py", "SegmentWriter.submit"),
    ("app/realtime/segment.py", "SegmentWriter.close"),
    ("app/realtime/archive.py", "EventArchive.append"),
    ("app/realtime/archive.py", "EventArchive.close"),
    ("app/realtime/archive.py", "EventArchive.read_verified"),
    ("app/realtime/archive.py", "EventArchive.read_unverified_diagnostic"),
    ("app/realtime/archive.py", "EventArchive.read_all"),
    ("app/realtime/archive.py", "EventArchive.verify"),
    ("app/realtime/archive.py", "replay"),
    ("app/realtime/archive.py", "reconcile_with_rest"),
    ("app/realtime/legacy_import.py", "scan_legacy"),
    ("app/realtime/legacy_import.py", "migrate_legacy_archive"),
    ("app/cli.py", "archive_init"),
    ("app/cli.py", "archive_recover_head"),
    ("app/cli.py", "archive_adopt"),
    ("app/cli.py", "archive_migrate_legacy"),
)

# The known finding this tool exists to reproduce and pin: A1's acceptance
# text requires it appear explicitly, so it is asserted, not merely hoped for.
#
# KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect A, deliberate ledger update:
# this chain was THE FINDING. `verify_segment` no longer calls `file_sha256`
# at all (both are replaced, at their one former call site, by
# `evidence_fs.stat_and_sha256_bounded`), and `file_sha256` itself no longer
# contains a raw `open(...)` (it now delegates to `evidence_fs.
# sha256_bounded`). The tuple is left unmodified as the frozen historical
# shape of the defect; the test that references it now asserts the chain is
# ABSENT from the current inventory, proving the fix rather than merely
# documenting the old defect.
REQUIRED_CHAIN = ("verify_segment", "file_sha256", "open(...)")

# --- manual bridge edges --------------------------------------------------
#
# A1's own docstring warns not to assume the shared abstraction is used
# merely because it exists, and the same discipline applies to THIS tool: a
# gap it cannot close by pure AST must be surfaced, not silently dropped.
# `EventArchive.append`/`.close` call `self._writer_for(when)` and then
# `writer.submit(...)`/`writer.close()` -- `writer`'s type is the RETURN
# VALUE of a method, which is whole-program type inference this tool does
# not implement (see `CallGraph._var_types_in`'s docstring: it only tracks
# `name = ClassName(...)` assignments, not `name = self.method(...)`).
# Reading `_writer_for`'s body/docstring confirms it always returns a
# `SegmentWriter`, so the edge below is added BY HAND, reviewed against the
# source, and recorded as `"manual": True` in the inventory so a reader can
# tell it apart from an AST-resolved one rather than mistaking it for a
# stronger guarantee than it is.
MANUAL_EDGES = {
    ("app/realtime/archive.py", "EventArchive.append"): [
        ("app/realtime/segment.py", "SegmentWriter.submit",
         "self._writer_for(when) always returns a SegmentWriter; see its "
         "constructor and docstring"),
    ],
    ("app/realtime/archive.py", "EventArchive.close"): [
        ("app/realtime/segment.py", "SegmentWriter.close",
         "self._writers[...]/self._retiring[...] values are always "
         "SegmentWriter instances, constructed in _writer_for"),
    ],
}

# --- primitive classification -------------------------------------------------

FS_OS_FUNCS = {"open", "scandir", "stat", "lstat", "readlink", "listdir", "walk",
              "fdopen", "replace", "unlink", "link", "rename", "remove",
              "mkdir", "chmod", "fsync", "close", "write"}
FS_PATH_METHODS = {"exists", "is_file", "is_dir", "is_symlink", "resolve",
                   "read_bytes", "read_text", "iterdir", "rglob", "glob",
                   "stat", "lstat", "open"}
MUTATION_OS_FUNCS = {"replace", "unlink", "link", "rename", "remove", "mkdir",
                     "chmod", "fsync", "write"}
HASH_NAMES = {"sha256", "update", "hexdigest", "file_sha256", "digest_hex"}
CANONICAL_NAMES = {"canonical_bytes", "parse_canonical", "canonical_datetime",
                   "canonicalize_or_reason", "non_canonical_reason",
                   "digest_hex"}
# Deliberately narrow: `get`, `put` and `join` collide too heavily with
# `dict.get`/`str.join`/thread `.join()` for a pure-AST, no-type-info sweep to
# call QUEUE-ACCOUNTING without producing more noise than signal. Restricted
# to method names that are distinctively `queue.Queue`'s own vocabulary.
QUEUE_NAMES = {"put_nowait", "get_nowait", "qsize", "task_done"}

EVIDENCE_FS_PRIMITIVES = {
    "presence": "FILESYSTEM ACCESS",
    "assert_contained": "FILESYSTEM ACCESS",
    "containment_reason": "FILESYSTEM ACCESS",
    "safe_enumerate": "FILESYSTEM ACCESS",
    "bounded_read": "FILESYSTEM ACCESS",
    "open_bounded_gzip": "FILESYSTEM ACCESS",
    "is_regular_file": "FILESYSTEM ACCESS",
}


def _classify_leaf(namespace: str, name: str) -> str | None:
    """Return a classification for a known primitive call, or None if this
    call is not a primitive leaf this tool recognises (in which case the
    caller should keep traversing, or -- if traversal is exhausted -- report
    it as UNCLASSIFIED rather than silently dropping it)."""
    if namespace == "evidence_fs" and name in EVIDENCE_FS_PRIMITIVES:
        return EVIDENCE_FS_PRIMITIVES[name]
    if namespace == "os" and name in MUTATION_OS_FUNCS:
        return "STATE MUTATION"
    if namespace == "os" and name in FS_OS_FUNCS:
        return "FILESYSTEM ACCESS"
    if namespace == "os.path":
        return "FILESYSTEM ACCESS"
    if namespace == "gzip":
        return "FILESYSTEM ACCESS"
    if namespace == "hashlib" or name in HASH_NAMES:
        return "HASHING"
    if name in CANONICAL_NAMES:
        return "CANONICAL ENCODING"
    if name in QUEUE_NAMES:
        return "QUEUE-ACCOUNTING"
    if namespace == "<builtin>" and name == "open":
        return "FILESYSTEM ACCESS"
    if namespace == "<path>" and name in FS_PATH_METHODS:
        return "FILESYSTEM ACCESS"
    if namespace == "fcntl":
        return "STATE MUTATION"
    return None


# --- AST symbol table ---------------------------------------------------------


class _ModuleInfo:
    def __init__(self, relpath: str, tree: ast.Module):
        self.relpath = relpath
        self.tree = tree
        # qualname -> ast.FunctionDef/AsyncFunctionDef
        self.functions: dict[str, ast.AST] = {}
        # every class name defined in this module, for local variable
        # type-inference (`writer = SegmentWriter(...)` -> `writer` is a
        # `SegmentWriter`).
        self.classes: set[str] = set()
        # alias -> module dotted path, for `import X as alias` / bare `import X`
        self.module_aliases: dict[str, str] = {}
        # local name -> (module dotted path, original name), for
        # `from X import Y [as Z]`
        self.name_imports: dict[str, tuple] = {}
        self._index()

    def _index(self):
        class _Collector(ast.NodeVisitor):
            def __init__(inner):
                inner.stack = []

            def visit_ClassDef(inner, node):
                self.classes.add(node.name)
                inner.stack.append(node.name)
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions[f"{node.name}.{child.name}"] = child
                inner.generic_visit(node)
                inner.stack.pop()

            def visit_FunctionDef(inner, node):
                if not inner.stack:
                    self.functions[node.name] = node
                # DO descend, even into an already-indexed top-level function's
                # body: many production functions in this codebase import
                # LAZILY, inside the function (`archive_init`'s
                # `from app.realtime.archive_head import ..., head_state`
                # sits inside the function, not the module preamble). A
                # visitor that only scanned module-level imports missed every
                # one of those and silently under-reported the entry point's
                # reachable primitives -- exactly the kind of blind spot this
                # milestone exists to find in a *test harness*, so it is
                # fixed here rather than left as an unexamined limitation.
                inner.generic_visit(node)

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Import(inner, node):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".")[0]
                    self.module_aliases[bound] = alias.name

            def visit_ImportFrom(inner, node):
                if node.module is None:
                    return
                for alias in node.names:
                    bound = alias.asname or alias.name
                    # `from app.realtime import evidence_fs` binds a MODULE
                    # alias, not a name import -- the submodule becomes
                    # importable as `evidence_fs.presence(...)`. This was
                    # previously computed (`full`) and then never used: every
                    # `from PACKAGE import SUBMODULE` was stored as a name
                    # import regardless, which made `_resolve_call` treat
                    # `evidence_fs.sha256_bounded(...)` as attribute access
                    # on an unresolvable imported NAME rather than as a call
                    # into a traversable module -- exactly backward for the
                    # one import shape (`from app.realtime import
                    # evidence_fs`) every canonical reader uses to reach the
                    # reviewed evidence-filesystem abstraction. Fixed here:
                    # if `PACKAGE.SUBMODULE` resolves to a module this tool
                    # can traverse, `bound` is a module alias for it, not a
                    # name import.
                    full = f"{node.module}.{alias.name}"
                    dotted_to_relpath = {
                        m.replace("/", ".").removesuffix(".py"): m
                        for m in TRAVERSABLE_MODULES
                    }
                    if full in dotted_to_relpath:
                        self.module_aliases[bound] = full
                    else:
                        self.name_imports[bound] = (node.module, alias.name)

        _Collector().visit(self.tree)


def _load_module(relpath: str) -> _ModuleInfo:
    source = (REPO_ROOT / relpath).read_text()
    tree = ast.parse(source, filename=relpath)
    return _ModuleInfo(relpath, tree)


class CallGraph:
    """Cross-module, best-effort call graph over `TRAVERSABLE_MODULES`."""

    def __init__(self, modules=TRAVERSABLE_MODULES):
        self.modules: dict[str, _ModuleInfo] = {m: _load_module(m) for m in modules}
        # dotted module path (e.g. "app.realtime.evidence_fs") -> relpath, for
        # modules we can actually traverse into.
        self._dotted_to_relpath = {
            m.replace("/", ".").removesuffix(".py"): m for m in modules
        }

    def _resolve_local_module_for(self, dotted: str) -> str | None:
        return self._dotted_to_relpath.get(dotted)

    def calls_in(self, relpath: str, qualname: str) -> list[ast.Call]:
        mod = self.modules[relpath]
        node = mod.functions.get(qualname)
        if node is None:
            return []
        out = []

        class _V(ast.NodeVisitor):
            def visit_Call(inner, n):
                out.append(n)
                inner.generic_visit(n)

            def visit_FunctionDef(inner, n):
                # Skip nested function DEFINITIONS bodies at the top: still
                # visit calls made when a nested/lambda function is invoked
                # from the outer scope, but do not double count -- generic
                # visit already walks into it, which is what we want (a
                # nested helper's filesystem call is still reachable from the
                # entry point).
                inner.generic_visit(n)

            visit_AsyncFunctionDef = visit_FunctionDef

        _V().visit(node)
        return out

    def _var_types_in(self, relpath: str, qualname: str) -> dict[str, tuple]:
        """`name -> (relpath_of_class, ClassName)` for simple local
        assignments `name = ClassName(...)` or `name = alias.ClassName(...)`
        found anywhere in the function body (a flat pass, not control-flow
        aware -- sufficient for this best-effort tool: it lets `writer =
        SegmentWriter(...)` followed later by `writer.submit(...)` resolve to
        `SegmentWriter.submit`, which pure name/attribute resolution cannot
        do, and closes a real gap where an entry point's actual write path
        went through an untyped local instead of `self.` or a module alias).
        """
        mod = self.modules[relpath]
        node = mod.functions.get(qualname)
        if node is None:
            return {}
        out: dict[str, tuple] = {}

        class _V(ast.NodeVisitor):
            def visit_Assign(inner, n):
                if (len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, ast.Call)):
                    func = n.value.func
                    cls_name = None
                    if isinstance(func, ast.Name):
                        cls_name = func.id
                        if cls_name in mod.classes:
                            out[n.targets[0].id] = (relpath, cls_name)
                        elif cls_name in mod.name_imports:
                            src_module, orig = mod.name_imports[cls_name]
                            target = self._resolve_local_module_for(src_module)
                            if (target is not None
                                    and orig in self.modules[target].classes):
                                out[n.targets[0].id] = (target, orig)
                    elif isinstance(func, ast.Attribute) and isinstance(
                            func.value, ast.Name):
                        base = func.value.id
                        if base in mod.module_aliases:
                            dotted = mod.module_aliases[base]
                            target = self._resolve_local_module_for(dotted)
                            if (target is not None
                                    and func.attr in self.modules[target].classes):
                                out[n.targets[0].id] = (target, func.attr)
                inner.generic_visit(n)

        _V().visit(node)
        return out

    def _resolve_call(self, relpath: str, call: ast.Call, enclosing_class: str | None,
                      var_types: dict | None = None):
        """Return one of:
        ("primitive", namespace, name, classification)
        ("local", relpath, qualname)          -- a resolvable in-graph function
        ("unresolved", description)           -- could not be resolved further
        """
        mod = self.modules[relpath]
        func = call.func

        if isinstance(func, ast.Name):
            name = func.id
            if name == "open":
                return ("primitive", "<builtin>", "open",
                        _classify_leaf("<builtin>", "open"))
            if name in mod.functions:
                return ("local", relpath, name)
            if name in mod.name_imports:
                src_module, orig = mod.name_imports[name]
                target_relpath = self._resolve_local_module_for(src_module)
                if target_relpath is not None:
                    if orig in self.modules[target_relpath].functions:
                        return ("local", target_relpath, orig)
                    cls = _classify_leaf(src_module.rsplit(".", 1)[-1], orig)
                    if cls:
                        return ("primitive", src_module, orig, cls)
                    return ("unresolved", f"{src_module}.{orig}(...) [imported name, "
                                          "not traversable further]")
                cls = _classify_leaf("canonical" if "canonical" in src_module else "",
                                    orig)
                if cls:
                    return ("primitive", src_module, orig, cls)
                return ("unresolved", f"{name}(...) [from {src_module} import {orig}]")
            return ("unresolved", f"{name}(...) [unbound name]")

        if isinstance(func, ast.Attribute):
            attr = func.attr
            value = func.value
            if isinstance(value, ast.Name):
                base = value.id
                if var_types and base in var_types:
                    vt_relpath, vt_class = var_types[base]
                    qualname = f"{vt_class}.{attr}"
                    if qualname in self.modules[vt_relpath].functions:
                        return ("local", vt_relpath, qualname)
                    return ("unresolved", f"{base}.{attr}(...) [inferred type "
                                          f"{vt_class}, but no such method "
                                          "indexed]")
                if base in ("self", "cls") and enclosing_class:
                    qualname = f"{enclosing_class}.{attr}"
                    if qualname in mod.functions:
                        return ("local", relpath, qualname)
                    return ("unresolved", f"self.{attr}(...) [method not indexed, "
                                          f"e.g. inherited]")
                if base in mod.module_aliases:
                    dotted = mod.module_aliases[base]
                    target_relpath = self._resolve_local_module_for(dotted)
                    if target_relpath is not None:
                        if attr in self.modules[target_relpath].functions:
                            return ("local", target_relpath, attr)
                        cls = _classify_leaf(dotted.rsplit(".", 1)[-1], attr)
                        if cls:
                            return ("primitive", dotted, attr, cls)
                    ns = dotted.rsplit(".", 1)[-1]
                    cls = _classify_leaf(ns, attr)
                    if cls:
                        return ("primitive", dotted, attr, cls)
                    if ns in ("os", "gzip", "hashlib", "fcntl") or dotted in (
                            "os", "gzip", "hashlib", "fcntl"):
                        return ("primitive", dotted, attr, "FILESYSTEM ACCESS"
                               if dotted != "hashlib" else "HASHING")
                    return ("unresolved", f"{base}.{attr}(...) [module alias "
                                          f"{dotted!r} not traversable]")
                if base == "os":
                    return ("primitive", "os", attr, _classify_leaf("os", attr)
                           or "FILESYSTEM ACCESS")
                if base == "gzip":
                    return ("primitive", "gzip", attr, "FILESYSTEM ACCESS")
                if base == "hashlib":
                    return ("primitive", "hashlib", attr, "HASHING")
                if base == "fcntl":
                    return ("primitive", "fcntl", attr, "STATE MUTATION")
                if base in mod.name_imports:
                    src_module, orig = mod.name_imports[base]
                    return ("unresolved", f"{base}.{attr}(...) [{base} is an "
                                          f"imported NAME {src_module}.{orig}, "
                                          "not a module -- calling .attr() on it "
                                          "is attribute access on an object, "
                                          "which this tool does not type]")
                # Local variable of unknown type (e.g. a `Path` instance) --
                # classify defensively if the attribute name matches a known
                # filesystem-primitive method name, since a mistyped guess
                # here should err toward OVER-reporting a filesystem primitive
                # rather than silently dropping a real one.
                cls = _classify_leaf("<path>", attr)
                if cls:
                    return ("primitive", "<local-var>", attr, cls)
                if attr in QUEUE_NAMES:
                    return ("primitive", "<local-var>", attr, "QUEUE-ACCOUNTING")
                return ("unresolved", f"{base}.{attr}(...) [receiver type unknown]")
            # e.g. chained calls `foo().bar()` -- receiver is itself a Call.
            cls = _classify_leaf("<path>", attr)
            if cls:
                return ("primitive", "<chained>", attr, cls)
            return ("unresolved", f"<expr>.{attr}(...) [chained call]")

        return ("unresolved", "<unrecognised call shape>")

    def trace_from(self, relpath: str, qualname: str, *, max_depth: int = 25):
        """BFS from one entry point. Returns dict with 'primitives' (list of
        {chain, namespace, name, classification}) and 'unresolved' (list of
        {chain, description}) -- both deduplicated by their full chain text,
        and 'visited' (the local functions the BFS actually walked, for
        auditability)."""
        start_class = qualname.split(".")[0] if "." in qualname else None
        frontier = [((relpath, qualname), (qualname,))]
        seen_nodes = {(relpath, qualname)}
        primitives, unresolved, visited = [], [], []
        depth = 0
        while frontier and depth < max_depth:
            depth += 1
            next_frontier = []
            for (r, q), chain in frontier:
                visited.append(f"{r}::{q}")
                enclosing_class = q.split(".")[0] if "." in q else None
                var_types = self._var_types_in(r, q)
                for call in self.calls_in(r, q):
                    resolved = self._resolve_call(r, call, enclosing_class, var_types)
                    kind = resolved[0]
                    if kind == "primitive":
                        _, namespace, name, cls = resolved
                        primitives.append({
                            "chain": list(chain) + [f"{namespace}.{name}(...)"
                                                    if namespace not in (
                                                        "<builtin>", "<local-var>",
                                                        "<chained>")
                                                    else f"{name}(...)"],
                            "namespace": namespace, "name": name,
                            "classification": cls, "lineno": call.lineno,
                        })
                    elif kind == "local":
                        _, nr, nq = resolved
                        node_key = (nr, nq)
                        new_chain = tuple(chain) + (nq,)
                        if node_key not in seen_nodes:
                            seen_nodes.add(node_key)
                            next_frontier.append((node_key, new_chain))
                    else:
                        _, desc = resolved
                        unresolved.append({
                            "chain": list(chain) + [desc], "lineno": call.lineno,
                        })
            frontier = next_frontier
        return {"primitives": primitives, "unresolved": unresolved,
               "visited": sorted(set(visited))}


def build_inventory() -> dict:
    """The A1 deliverable: every entry point's reachable primitive set."""
    graph = CallGraph()
    entries = {}
    for relpath, qualname in ENTRY_POINTS:
        result = graph.trace_from(relpath, qualname)
        key = f"{relpath}::{qualname}"
        # Dedup primitives by (namespace, name, classification) for a stable,
        # reviewable list; keep ONE example chain per unique primitive.
        by_prim: dict[tuple, dict] = {}
        for p in result["primitives"]:
            k = (p["namespace"], p["name"], p["classification"])
            if k not in by_prim:
                by_prim[k] = dict(p, manual=False)
        manual_bridges = []
        for target_relpath, target_qualname, reason in MANUAL_EDGES.get(
                (relpath, qualname), []):
            manual_bridges.append({
                "target": f"{target_relpath}::{target_qualname}", "reason": reason})
            bridged = graph.trace_from(target_relpath, target_qualname)
            for p in bridged["primitives"]:
                k = (p["namespace"], p["name"], p["classification"])
                if k not in by_prim:
                    by_prim[k] = dict(
                        p, manual=True,
                        chain=[qualname, f"[MANUAL] {target_qualname}"] + p["chain"][1:])
        entries[key] = {
            "entrypoint": key,
            "primitives": sorted(
                ({"namespace": v["namespace"], "name": v["name"],
                  "classification": v["classification"],
                  "example_chain": v["chain"], "manually_bridged": v["manual"]}
                 for v in by_prim.values()),
                key=lambda d: (d["namespace"], d["name"], d["classification"])),
            "unresolved_calls": sorted(
                {tuple(u["chain"]) for u in result["unresolved"]}),
            "functions_visited": result["visited"],
            "manual_bridges": manual_bridges,
        }
    return {"schema_version": 1, "entry_points": entries}


def find_chain(inventory: dict, entrypoint_suffix: str, primitive_name: str) -> list | None:
    """Locate one entry point's example chain to a named primitive, by
    suffix match on the entry point key (e.g. "verify_segment") and exact
    match on the primitive's `name` (e.g. "open")."""
    for key, entry in inventory["entry_points"].items():
        if key.endswith(f"::{entrypoint_suffix}"):
            for prim in entry["primitives"]:
                if prim["name"] == primitive_name:
                    return prim["example_chain"]
    return None


INVENTORY_PATH = Path(__file__).parent / "inventory.json"


def load_committed_inventory() -> dict:
    return json.loads(INVENTORY_PATH.read_text())


if __name__ == "__main__":
    inv = build_inventory()
    INVENTORY_PATH.write_text(json.dumps(inv, indent=2, sort_keys=True) + "\n")
    print(f"wrote {INVENTORY_PATH}")
