"""Structural source guards that can tell an assertion from a violation.

Six times in this milestone a substring scan has condemned a module for
containing the very sentence that promises the property under test:

    "no RPC response can emit CANONICALLY_VERIFIED"
    "this tool reads market timing and nothing else -- no price, no volume"
    "never merged, split, reweighted or dropped"
    "TTE heterogeneity lives in a separate module"
    `ProvenanceScope.QUOTED` matched a search for market "quote"
    `SessionRecord.label` matched a search for research "label"

The rule in TESTING_POLICY is to guard over structure. This is that, written
once so it stops being rewritten badly.
"""

from __future__ import annotations

import ast
import inspect


def _docstrings(tree: ast.AST) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d:
                out.add(d)
    return out


def referenced_names(obj, *, include_literals: bool = True) -> set[str]:
    """Identifiers and non-docstring literals the code actually USES."""
    tree = ast.parse(inspect.getsource(obj))
    docs = _docstrings(tree)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    if include_literals:
        names |= {n.value for n in ast.walk(tree)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and n.value not in docs}
    return {n.lower() for n in names}


def imported_modules(obj) -> set[str]:
    tree = ast.parse(inspect.getsource(obj))
    mods = {n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
             for a in n.names}
    return {m.lower() for m in mods}


#: A literal long enough to be a sentence is prose, not data. Module notes,
#: error messages and refusal explanations routinely NAME the thing they
#: promise not to compute -- "no return, markout, price response ... is
#: computed here" -- and a guard that reads them as usage condemns the module
#: for documenting itself. Identifiers and short literals (dict keys, method
#: names, enum values) are what can actually carry a forbidden concept.
PROSE_MIN_CHARS = 40


def is_prose(literal: str) -> bool:
    return len(literal) >= PROSE_MIN_CHARS and " " in literal.strip()


def assert_never_references(obj, banned, *, allow=(), literals=True):
    """Fail if the CODE references a banned concept. Prose is not code.

    Prose literals are excluded on the same principle as docstrings: a module
    that says "no price is computed here" is asserting the property, not
    violating it.
    """
    allowed = {a.lower() for a in allow}
    used = {u for u in referenced_names(obj, include_literals=literals)
            if not is_prose(u)} - allowed
    for b in banned:
        hits = {u for u in used if b.lower() in u}
        assert not hits, f"{getattr(obj, '__name__', obj)} references {sorted(hits)}"
