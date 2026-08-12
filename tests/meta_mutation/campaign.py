#!/usr/bin/env python3
"""A9 -- the mutation catalogue and the apply/run/restore machinery.

Re-runnable standalone (`python3 tests/meta_mutation/campaign.py`) or
through pytest (`tests/test_kalshi_meta_mutation_campaign_001.py`, which
parametrizes over `MUTATIONS` so each one is its own reportable test node).
Both paths use exactly this module's `apply_mutation`/`restore_mutation`/
`run_catch_command` -- there is only one implementation of "how a mutation
is applied and undone", not one for humans and a different one for CI.

Each `Mutation` names:
  * WHERE     a single, EXACT, byte-for-byte substring replacement in one
              file under `tests/` (never `app/`).
  * WHAT      the class of verification-infrastructure weakening it
              performs (matches the milestone's numbered list).
  * PROOF     the exact `pytest` node id(s) (or the module's own `-k`
              filters) that are expected to FAIL once the mutation is
              applied, and to PASS again once it is restored.

`old` must appear EXACTLY ONCE in the target file at the moment a mutation
is applied -- `apply_mutation` asserts this rather than silently patching
the wrong (or every) occurrence, since a `.replace()` with `count=1` on
non-unique text is a silent correctness bug in the harness itself.

WHAT A "10/10 CAUGHT" RESULT DOES AND DOES NOT MEAN
(KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 A8 -- stated here, at the source of
the figure, because it was being read as more than it is.)

EVERY mutation in this catalogue targets a file under `tests/`. NOT ONE
touches `app/`. A clean sweep therefore proves exactly one thing: the
verification layer's own guard vocabulary is intact -- weaken a harness and
a named test goes red. That is worth having, and it is ALL it is.

It proves NOTHING about `app/realtime/segment.py`: nothing about
`submit()`'s or `close()`'s logic, nothing about signal handling at the
commitment point, nothing about the work-budget reserves, nothing about
residue classification. Three independent reviewers found real defects in
exactly those places while this campaign was reporting a clean sweep --
which is the demonstration rather than the argument.

Do not cite this number as evidence that a production fix is correct.
Production behaviour is pinned by tests that assert against `app/`
directly and were each verified to FAIL with their fix reverted -- see
`tests/test_kalshi_archive_replay_integrity_a8_001.py`.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Mutation:
    id: str
    category: str
    description: str
    target: str                 # repo-relative path
    old: str
    new: str
    catch_args: tuple           # args appended to `python3 -m pytest -q`
    rationale: str


def _read(target: str) -> str:
    return (REPO_ROOT / target).read_text()


def _write(target: str, text: str) -> None:
    (REPO_ROOT / target).write_text(text)


def apply_mutation(mutation: Mutation) -> str:
    """Apply `mutation` to its target file. Returns the ORIGINAL text, which
    the caller MUST pass to `restore_mutation` -- this module never keeps
    its own hidden backup registry, so a caller that discards the return
    value cannot accidentally restore from stale state."""
    original = _read(mutation.target)
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        raise AssertionError(
            f"mutation {mutation.id!r}: expected `old` to appear exactly "
            f"once in {mutation.target}, found {occurrences} -- the target "
            "file has drifted since this mutation was written; update "
            "`old`/`new` deliberately rather than patching the wrong "
            "occurrence")
    mutated = original.replace(mutation.old, mutation.new, 1)
    _write(mutation.target, mutated)
    return original


def restore_mutation(mutation: Mutation, original_text: str) -> None:
    _write(mutation.target, original_text)
    restored = _read(mutation.target)
    if restored != original_text:
        raise AssertionError(
            f"mutation {mutation.id!r}: restore did not round-trip byte-"
            f"for-byte on {mutation.target} -- the working tree may now "
            "differ from its pre-campaign state and needs manual review")


def run_catch_command(mutation: Mutation, *, timeout_s: float = 180.0):
    cmd = [sys.executable, "-m", "pytest", "-q", *mutation.catch_args]
    return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=timeout_s)


# ---------------------------------------------------------------------------
# THE CATALOGUE. One entry per numbered item in the milestone's mutation
# list, plus additional items judged necessary for full A1-A8 gate coverage.
# ---------------------------------------------------------------------------

MUTATIONS: tuple = (
    Mutation(
        id="M1_guard_drop_api_name",
        category="static guard vocabulary",
        description='remove "read_text" from the consolidated AST guard\'s '
                    "PATH_METHOD_NAMES detection set",
        target="tests/harness_filesystem_totality/ast_audit.py",
        old='    "read_bytes", "read_text", "iterdir", "rglob", "open", "stat", "lstat",\n',
        new='    "read_bytes", "iterdir", "rglob", "open", "stat", "lstat",\n',
        catch_args=(
            "tests/test_kalshi_meta_inventory_001.py::TestA2RedTeamGuard::"
            "test_the_one_guard_discriminates_the_entire_corpus_both_directions",
        ),
        rationale='drops the guard\'s ability to see `Path(p).read_text()` -- '
                 "the red-team corpus's `path_read_text` entry must flip from "
                 "caught to bypassed and fail the both-directions "
                 "discrimination test.",
    ),
    Mutation(
        id="M2_call_graph_drop_entry_point",
        category="production API inventory (A1)",
        description="remove verify_segment from the A1 call-graph's "
                    "ENTRY_POINTS enumeration",
        target="tests/meta_inventory/call_graph.py",
        old='    ("app/realtime/segment.py", "verify_segment"),\n',
        new="",
        catch_args=(
            "tests/test_kalshi_meta_inventory_001.py::TestA1CallGraphInventory",
        ),
        rationale="the inventory would silently stop tracing production's "
                 "own most security-relevant read entry point -- must fail "
                 "both the entry-point-enumeration test and the committed-"
                 "artifact drift test (verify_segment's chain to file_sha256 "
                 "-> open() disappears from the regenerated inventory).",
    ),
    Mutation(
        id="M3_parent_timeout_weakened",
        category="parent-enforced timeout (A4)",
        description="a genuine subprocess hang, once SIGKILLed by the "
                    "parent, is reported as COMPLETED instead of "
                    "TIMEOUT_FAIL",
        target="tests/meta_runtime/parent_timeout.py",
        old='        return TimeoutVerdict("TIMEOUT_FAIL", proc.returncode, duration,\n'
           '                              out or "", err or "", killed_via, proc.pid)',
        new='        return TimeoutVerdict("COMPLETED", proc.returncode, duration,\n'
           '                              out or "", err or "", killed_via, proc.pid)',
        catch_args=(
            "tests/test_kalshi_meta_runtime_parent_timeout_001.py::"
            "TestPositiveDirectionFiveHangShapes::"
            "test_kills_a_known_infinite_sequence",
        ),
        rationale="the process is STILL actually killed (no leaked hung "
                 "child) -- only the VERDICT is misreported, exactly the "
                 "shape of 'the deadline fired but the caller is told "
                 "nothing happened' this module exists to prevent.",
    ),
    Mutation(
        id="M4_aggregate_work_bound_vacuous",
        category="aggregate-work property (A5)",
        description="GoodEncoderWithAggregateBound's declared total-visit "
                    "ceiling never actually fires",
        target="tests/meta_runtime/aggregate_work.py",
        old="        if self.visits > self.MAX_TOTAL_VISITS:\n"
           "            raise AggregateWorkExceeded(",
        new="        if False:  # MUTATION: aggregate bound disabled\n"
           "            raise AggregateWorkExceeded(",
        catch_args=(
            "tests/test_kalshi_meta_runtime_aggregate_work_001.py::"
            "TestDiscriminationPlantedEncoders::"
            "test_good_encoder_with_a_declared_aggregate_bound_satisfies_the_property",
        ),
        rationale="the property test's own COMPLIANT reference encoder must "
                 "still be proven to raise `AggregateWorkExceeded` at the "
                 "reviewer's depth-61 reproduction, or the property itself "
                 "is unfalsifiable.",
    ),
    Mutation(
        id="M5_independent_accounting_made_tautological",
        category="independent accounting (A1/A7)",
        description="reconcile() derives its match/gap verdict from the "
                    "writer's OWN internal consistency check "
                    "(WriterAccounting.disposition_holds(), true BY "
                    "CONSTRUCTION per A6) instead of independently "
                    "comparing two separately-sourced observations",
        target="tests/meta_runtime/independent_accounting.py",
        old="def reconcile(ledger: AdmissionLedger, writer) -> ReconciliationReport:\n"
           '    """THE independent check. Never reads `writer.accounting.accepted`,\n'
           "    `.written`, or calls `writer.accounting.disposition_holds()`/\n"
           "    `.reconciles()` -- those are the tautology this function exists to be\n"
           '    independent OF. Compares the producer-observed ledger against a fresh\n'
           '    re-decode of the file on disk instead.\n'
           '    """\n'
           "    durable = read_durable_disposition(writer)\n"
           "    gap = ledger.accepted - durable.total\n"
           "    return ReconciliationReport(ledger.accepted, durable.total, gap == 0, gap)",
        new="def reconcile(ledger: AdmissionLedger, writer) -> ReconciliationReport:\n"
           "    # MUTATION: derives the match verdict from WriterAccounting's own\n"
           "    # disposition_holds() -- ALWAYS True by construction (accepted IS\n"
           "    # written + failed_after_accept, so it cannot disagree with itself)\n"
           "    # -- the exact tautology this function exists to be independent of.\n"
           "    durable = read_durable_disposition(writer)\n"
           "    matches = writer.accounting.disposition_holds()\n"
           "    gap = 0 if matches else (ledger.accepted - durable.total)\n"
           "    return ReconciliationReport(ledger.accepted, durable.total, matches, gap)",
        catch_args=(
            "tests/test_kalshi_meta_runtime_independent_accounting_001.py::"
            "TestReconcileItselfCatchesAGenuineGapDispositionHoldsCannotSee::"
            "test_a_genuinely_corrupted_file_produces_a_real_gap_disposition_holds_misses",
        ),
        rationale="`WriterAccounting.disposition_holds()` is TRUE BY "
                 "CONSTRUCTION (`accepted` is DEFINED as `written + "
                 "failed_after_accept`, so it can never disagree with "
                 "itself) -- it therefore stays True even while a genuinely "
                 "corrupted file leaves the ledger and an independent "
                 "re-decode disagreeing on record count, and the mutated "
                 "reconcile() reports `matches: True` over a real gap.",
    ),
    Mutation(
        id="M6_duplicate_key_check_removed",
        category="mapping semantics (A6)",
        description="the duplicate-key classifier's own detection is "
                    "hard-wired empty, making SILENTLY_LOSSY unreachable",
        target="tests/test_kalshi_meta_runtime_mapping_semantics_001.py",
        old="    dup_keys = {k: vs for k, vs in values_by_key.items()\n"
           "               if len(vs) > 1 and len(set(vs)) > 1}",
        new="    dup_keys = {}  # MUTATION: duplicate-key detection removed",
        catch_args=(
            "tests/test_kalshi_meta_runtime_mapping_semantics_001.py::"
            "TestProductionIsSilentlyLossy",
        ),
        rationale="every case (including the real, pinned production "
                 "defect) classifies NOT_APPLICABLE instead of "
                 "SILENTLY_LOSSY -- the classifier stops classifying "
                 "anything.",
    ),
    Mutation(
        id="M7_argument_matrix_drops_root_none",
        category="public argument-shape matrix (A3)",
        description='the "none" root_mode is removed from A3\'s ROOT_MODES, '
                    "silently dropping the milestone's named finding "
                    "(verify_segment(root=None)) from the matrix",
        target="tests/meta_inventory/argument_matrix.py",
        old='ROOT_MODES = ("omit", "explicit_correct", "explicit_wrong", "none")',
        new='ROOT_MODES = ("omit", "explicit_correct", "explicit_wrong")  # MUTATION: root=None dropped',
        catch_args=(
            "tests/test_kalshi_meta_inventory_001.py::TestA3ArgumentShapeMatrix",
        ),
        rationale="both the cell-count test (28 -> 21) and the test that "
                 "asserts root=None hangs on the poisoned fixture (KeyError: "
                 "'none' is no longer a cell at all) must fail.",
    ),
    Mutation(
        id="M8_matrix_driver_swallows_typed_failure",
        category="typed failure surfaced -> silent continue",
        description="the A3 child-process driver's top-level exception "
                    "reporting is replaced with a silent pass, so a raised "
                    "exception is never reported back to the parent at all",
        target="tests/meta_inventory/matrix_cell_driver.py",
        old='    except BaseException as exc:            # noqa: BLE001 - reported, not hidden\n'
           '        result["raised"] = True\n'
           '        result["exception_type"] = type(exc).__name__\n'
           '        result["exception_module"] = type(exc).__module__\n'
           '        result["exception_message"] = str(exc)[:2000]',
        new='    except BaseException:                    # MUTATION: swallowed silently\n'
           '        pass',
        catch_args=(
            "tests/test_kalshi_meta_inventory_001.py::TestA3ArgumentShapeMatrix::"
            "test_driver_reports_typed_failures_not_silence",
        ),
        rationale="a driver that swallows its own exceptions turns EVERY "
                 "matrix cell that should classify RAISED into a bogus "
                 "RETURNED with `detail=None` -- silent, not a hang, not a "
                 "crash, and invisible to the existing hung/crashed checks; "
                 "needs its own direct unit test of driver fidelity rather "
                 "than hoping a matrix cell happens to exercise it.",
    ),
    Mutation(
        id="M9_strict_xfail_forced_always",
        category="strict-xfail semantic integrity",
        description="the encoder-fidelity harness's one strict-xfail test "
                    "is given an unconditional escape hatch that fires "
                    "regardless of whether the underlying defect still "
                    "reproduces",
        target="tests/test_kalshi_encoder_fidelity_harness_001.py",
        old="    def test_admitted_implies_fixpoint(self):\n"
           "        d = {_CollidingKey(\"a\"): 1, _CollidingKey(\"a\"): 2}\n"
           "        assert sg.non_canonical_reason(d) is None\n"
           "        cn.assert_fixpoint(d)   # raises CanonicalError today -- that's the gap",
        new="    def test_admitted_implies_fixpoint(self):\n"
           "        import pytest as _pytest\n"
           "        _pytest.xfail(\"MUTATION: forced, unconditional escape hatch\")\n"
           "        d = {_CollidingKey(\"a\"): 1, _CollidingKey(\"a\"): 2}\n"
           "        assert sg.non_canonical_reason(d) is None\n"
           "        cn.assert_fixpoint(d)   # raises CanonicalError today -- that's the gap",
        catch_args=(
            "tests/test_kalshi_meta_mutation_campaign_001.py::"
            "test_strict_xfail_tests_have_no_unconditional_escape_hatch",
        ),
        rationale="pytest's outcome (xfailed) is IDENTICAL whether the test "
                 "body actually exercised the real assertion or short-"
                 "circuited before reaching it -- the suite's pass/fail "
                 "colour cannot distinguish 'still-reproducing known defect' "
                 "from 'test body neutered'; needs a SOURCE-level check, not "
                 "a pytest-outcome-level one.",
    ),
    Mutation(
        id="M10_durable_disposition_reads_the_tautological_source",
        category="independent accounting, second source (A1/A7)",
        description="read_durable_disposition() reads writer.accounting."
                    "written -- the SAME counter a bookkeeping-only defect "
                    "would corrupt -- instead of independently re-decoding "
                    "the file on disk",
        target="tests/meta_runtime/independent_accounting.py",
        old="    from app.realtime import segment as sg\n\n"
           "    records = sg.read_segment_records(writer.events_path)\n"
           "    return DurableDisposition(on_disk_records=len(records))",
        new="    # MUTATION: reads the writer's OWN counter instead of the file --\n"
           "    # no longer a second, independent source.\n"
           "    return DurableDisposition(on_disk_records=writer.accounting.written)",
        catch_args=(
            "tests/test_kalshi_meta_runtime_independent_accounting_001.py::"
            "TestPlantedBadAccountingFailsTheMetaTest::"
            "test_a_bookkeeping_only_increment_fools_disposition_holds_but_not_reconcile",
        ),
        rationale="the whole point of `read_durable_disposition` is that it "
                 "is sourced from the FILE, never from `writer.accounting` "
                 "in any form -- collapsing it onto `writer.accounting."
                 "written` means a planted, bookkeeping-only increment to "
                 "that SAME field is now invisible to both sides of the "
                 "reconciliation at once, and the test's own assertion "
                 "that `report.durable_total == 4` (the true on-disk count, "
                 "not the corrupted 5) fails.",
    ),
)


def run_campaign(mutations=MUTATIONS, *, verbose: bool = True) -> list:
    """Apply, prove-catches, restore -- one mutation at a time, never two
    mutations live at once. Returns a list of result dicts. Raises (after
    best-effort restore) if a restore itself fails to round-trip, since a
    left-mutated working tree is worse than a failed campaign run."""
    results = []
    for m in mutations:
        original = apply_mutation(m)
        try:
            proc = run_catch_command(m)
            caught = proc.returncode != 0
        finally:
            restore_mutation(m, original)
        result = {
            "id": m.id, "category": m.category, "caught": caught,
            "catch_args": m.catch_args, "returncode": proc.returncode,
        }
        results.append(result)
        if verbose:
            status = "CAUGHT" if caught else "**HOLE**"
            print(f"[{status}] {m.id} ({m.category})")
    return results


if __name__ == "__main__":
    results = run_campaign()
    holes = [r for r in results if not r["caught"]]
    print(f"\n{len(results)} mutations run, {len(holes)} hole(s).")
    if holes:
        for h in holes:
            print(f"  HOLE: {h['id']} -- {h['catch_args']} stayed green")
        sys.exit(1)
    sys.exit(0)
