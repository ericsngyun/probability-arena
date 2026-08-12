"""KALSHI-ARCHIVE-VERIFICATION-META-001 A9 -- the mutation campaign, as a
committed, re-runnable pytest suite.

Each parametrized case: apply ONE mutation from `tests/meta_mutation.
campaign.MUTATIONS` to the VERIFICATION INFRASTRUCTURE (never `app/`), run
the narrow pytest target that mutation names as its own "this had better go
red" proof, assert it DID go red, then restore the original file bytes
(always, even on failure -- see the `finally` below) and assert the restore
round-tripped exactly.

A test in this file failing has TWO possible readings and they are not the
same:

  * the CATCH assertion fails -- the mutation left its target test green.
    This is a HOLE in the verification layer: report it, and (as this file
    already does for the two mutations the campaign actually found holes
    for -- see `test_driver_reports_typed_failures_not_silence` in
    `test_kalshi_meta_inventory_001.py` and
    `test_strict_xfail_tests_have_no_unconditional_escape_hatch` below)
    strengthen the target until it is caught.
  * the RESTORE assertion fails -- something is wrong with THIS harness's
    own file-handling, not with the thing being tested; treat it as a bug
    in `tests/meta_mutation/campaign.py`, not a production finding.

Mutations run ONE AT A TIME (never two mutated files live simultaneously),
serially, so a failure in one case's restore cannot cascade into a false
catch/hole verdict for the next.

ISOLATION (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001). Every case below mutates a
DISPOSABLE COPY of the repository, never the live working tree -- because
this module parametrizes over `MUTATIONS`, the previous design meant every
full-suite run edited production test files in place, and an interrupted
one left them edited. The module-scoped `sandbox` fixture materialises one
copy for the whole file (cleaned up on success, PRESERVED and printed if
any test in this module failed), and the autouse `_live_tree_tripwire`
fixture censuses `tests/` and `app/` before and after EVERY test so an
isolation regression fails here rather than silently corrupting the tree.
See `tests/meta_mutation/campaign.py`'s module docstring for the design and
`tests/test_kalshi_meta_mutation_isolation_001.py` for the proofs (SIGINT,
SIGKILL, and two concurrent runs).

SCOPE, STATED PLAINLY (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8). Every
mutation here targets `tests/`; none targets `app/`. A full-green run means
"the verification layer's guard vocabulary is intact", and nothing more. It
is NOT evidence that any production behaviour is correct and must never be
cited as such -- three reviewers found real `segment.py` defects while this
campaign was green. See `tests/meta_mutation/campaign.py`'s module
docstring for the full statement, and
`tests/test_kalshi_archive_replay_integrity_a8_001.py` for the tests that
DO pin production behaviour (each verified to fail with its fix reverted).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from tests.meta_mutation import campaign as camp


@pytest.fixture(scope="module")
def sandbox(request):
    """One disposable repository copy for this whole module.

    Deleted when every test in the module passed; on ANY failure here it is
    left in place and its path printed, because the mutated-and-restored
    copy is the evidence you need to debug a HOLE or a restore fault.
    """
    root = camp.create_sandbox(label="pytest")
    failed_before = request.session.testsfailed
    try:
        yield root
    finally:
        if request.session.testsfailed > failed_before:
            print(f"\nmeta-mutation: sandbox PRESERVED for debugging: {root}")
        else:
            camp.destroy_sandbox(root)


@pytest.fixture(autouse=True)
def _live_tree_tripwire():
    """Defence in depth: isolation can regress, so census the live tree
    before and after EVERY test in this module and fail loudly, naming the
    offending paths, rather than letting a corrupted tree out of here."""
    camp.assert_live_tree_clean("preflight")
    before = camp.live_tree_census()
    yield
    camp.assert_live_tree_unchanged(before, "postflight")
    camp.assert_live_tree_clean("postflight")


@pytest.mark.parametrize(
    "mutation", camp.MUTATIONS, ids=[m.id for m in camp.MUTATIONS])
def test_mutation_is_caught_by_its_named_proof(mutation, sandbox):
    original = camp.apply_mutation(mutation, root=sandbox)
    try:
        proc = camp.run_catch_command(mutation, root=sandbox)
        caught = proc.returncode != 0
    finally:
        camp.restore_mutation(mutation, original, root=sandbox)
    assert caught, (
        f"HOLE: mutation {mutation.id!r} ({mutation.category}) left "
        f"{mutation.catch_args!r} GREEN (returncode=0). "
        f"stdout tail:\n{proc.stdout[-3000:]}\nstderr tail:\n{proc.stderr[-1500:]}")


def test_every_mutation_id_is_unique():
    ids = [m.id for m in camp.MUTATIONS]
    assert len(ids) == len(set(ids)), f"duplicate mutation ids: {ids}"


def test_every_mutation_targets_a_file_under_tests_never_app():
    for m in camp.MUTATIONS:
        assert m.target.startswith("tests/"), (
            f"mutation {m.id!r} targets {m.target!r} -- the mutation "
            "campaign must only ever touch verification infrastructure "
            "under tests/, never app/")
        assert not (camp.REPO_ROOT / m.target).is_relative_to(
            camp.REPO_ROOT / "app"), (
            f"mutation {m.id!r} resolves under app/ -- absolute constraint "
            "violation")


def test_campaign_leaves_the_working_tree_unchanged_when_run_end_to_end():
    """The end-to-end guarantee the whole campaign depends on: running
    EVERY mutation, one at a time, leaves the LIVE working tree byte-
    identical to how it started. Runs the catch commands too (not just
    apply/restore) so this also proves the guarantee survives a mutation
    that was actually exercised, not merely written and immediately
    reverted.

    Censuses `tests/` AND `app/` in full -- not merely the catalogue's ten
    target files -- because the failure this exists to catch is "isolation
    regressed and the harness wrote somewhere in the live tree", and the
    old target-file-only comparison could not have seen that.
    """
    before = camp.live_tree_census()
    assert before, "the live-tree census found no files -- it is not looking"
    camp.run_campaign(camp.MUTATIONS, verbose=False)
    after = camp.live_tree_census()
    assert before == after, (
        "the LIVE working tree changed after a full campaign run: "
        f"{sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))}")


def test_campaign_never_writes_into_the_live_working_tree():
    """The structural guarantee, asserted directly: every write path is
    gated on `_sandbox_root`, and pointing it at the live tree (or at an
    ancestor of it, or at a plain directory with no sandbox marker) raises
    instead of writing. This is what makes "the harness corrupted the repo"
    a raised exception rather than a possible outcome.
    """
    m = camp.MUTATIONS[0]
    for bad_root in (camp.REPO_ROOT,
                     camp.REPO_ROOT / "tests",
                     camp.REPO_ROOT.parent):
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp.apply_mutation(m, root=bad_root)
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp.restore_mutation(m, "whatever", root=bad_root)
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp.run_catch_command(m, root=bad_root)


def test_a_directory_without_the_sandbox_marker_is_not_a_sandbox(tmp_path):
    """An unmarked directory -- e.g. a half-built copy, or someone's own
    scratch checkout -- must not be accepted as a mutation target."""
    (tmp_path / "tests").mkdir()
    with pytest.raises(camp.LiveTreeWriteBlocked):
        camp._sandbox_root(tmp_path)


# =====================================================================
# M9's catching mechanism: a SOURCE-level check, because pytest's own
# xfailed/failed/passed OUTCOME cannot distinguish "the known defect still
# reproduces" from "the test body was neutered with an unconditional
# escape hatch before it ever reached the real assertion" -- both report
# as `xfailed` under `strict=True`. This has to inspect the AST of every
# strict-xfail test's body, not merely trust that it ran.
# =====================================================================

_ESCAPE_HATCH_CALLS = {"xfail", "skip", "exit"}


def _calls_unconditional_escape_hatch(func) -> bool:
    """True if `func`'s body contains a top-level (not nested inside an
    `if`/`for`/`while`/`try` guard) call to `pytest.xfail`/`pytest.skip`/
    `pytest.exit`/a bare `xfail`/`skip` -- the shape that forces an
    xfail/skip outcome regardless of what runs afterward. Deliberately
    walks only the function's IMMEDIATE statement list (not nested blocks),
    since a conditional call (`if some_real_check(): pytest.xfail(...)`) is
    a legitimate, data-dependent skip, not the unconditional-escape-hatch
    mutation this exists to catch.
    """
    src = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(src)
    func_def = tree.body[-1]
    assert isinstance(func_def, (ast.FunctionDef, ast.AsyncFunctionDef))
    for stmt in func_def.body:
        for node in ast.walk(stmt) if isinstance(stmt, ast.Expr) else []:
            if isinstance(node, ast.Call):
                callee = node.func
                name = None
                if isinstance(callee, ast.Attribute):
                    name = callee.attr
                elif isinstance(callee, ast.Name):
                    name = callee.id
                if name in _ESCAPE_HATCH_CALLS:
                    return True
    return False


def test_strict_xfail_tests_have_no_unconditional_escape_hatch():
    """Every `@pytest.mark.xfail(strict=True, ...)` test in the encoder-
    fidelity harness must reach its REAL assertion, not a forced
    `pytest.xfail(...)`/`pytest.skip(...)` planted before it. This is the
    catching mechanism `Mutation M9_strict_xfail_forced_always` names --
    written as a permanent, always-on regression test (not merely invoked
    from inside the mutation-campaign harness) because a neutered
    strict-xfail test is exactly the kind of change a normal code review
    could plausibly wave through: the suite stays green either way.
    """
    from tests import test_kalshi_encoder_fidelity_harness_001 as enc

    checked = 0
    offenders = []
    for name, obj in vars(enc).items():
        if not inspect.isclass(obj):
            continue
        for attr_name, attr in vars(obj).items():
            if not (attr_name.startswith("test_") and callable(attr)):
                continue
            marker = getattr(attr, "pytestmark", None)
            if not marker:
                continue
            is_strict_xfail = any(
                m.name == "xfail" and m.kwargs.get("strict") is True
                for m in marker)
            if not is_strict_xfail:
                continue
            checked += 1
            if _calls_unconditional_escape_hatch(attr):
                offenders.append(f"{name}.{attr_name}")
    assert checked >= 1, (
        "expected to find at least one strict-xfail test in the encoder "
        "fidelity harness to check -- if this is now 0, either the harness "
        "changed shape or this test's discovery logic broke")
    assert not offenders, (
        f"strict-xfail test(s) with an unconditional escape hatch before "
        f"their real assertion: {offenders}")
