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
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from tests.meta_mutation import campaign as camp


@pytest.mark.parametrize(
    "mutation", camp.MUTATIONS, ids=[m.id for m in camp.MUTATIONS])
def test_mutation_is_caught_by_its_named_proof(mutation):
    original = camp.apply_mutation(mutation)
    try:
        proc = camp.run_catch_command(mutation)
        caught = proc.returncode != 0
    finally:
        camp.restore_mutation(mutation, original)
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
    EVERY mutation, one at a time, leaves every target file byte-identical
    to how it started. Runs the catch commands too (not just apply/restore)
    so this also proves restore survives a mutation that was actually
    exercised, not merely written and immediately reverted.
    """
    before = {m.target: camp._read(m.target) for m in camp.MUTATIONS}
    camp.run_campaign(camp.MUTATIONS, verbose=False)
    after = {m.target: camp._read(m.target) for m in camp.MUTATIONS}
    assert before == after, "the working tree changed after a full campaign run"


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
