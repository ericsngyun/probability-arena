#!/usr/bin/env python3
"""A9 -- the mutation catalogue and the apply/run/restore machinery.

Re-runnable standalone (`python3 tests/meta_mutation/campaign.py`) or
through pytest (`tests/test_kalshi_meta_mutation_campaign_001.py`, which
parametrizes over `MUTATIONS` so each one is its own reportable test node).
Both paths use exactly this module's `apply_mutation`/`restore_mutation`/
`run_catch_command` -- there is only one implementation of "how a mutation
is applied and undone", not one for humans and a different one for CI.

ISOLATION: NOTHING HERE EVER WRITES TO THE LIVE WORKING TREE
(KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 -- added after this harness corrupted
the tree it was supposed to be validating.)

An earlier revision applied each mutation directly to the file under
`REPO_ROOT`, ran the catch command, and undid it in a `finally`. That is a
verification harness that modifies the very files it is verifying, and it
failed in exactly the way that design must:

  * an interrupted run (an orphaned reviewer process) left
    `tests/meta_runtime/aggregate_work.py` mutated on disk, restore never
    having run -- no `finally` survives SIGKILL, and there was no lock
    file, no PID file, and no cleanliness preflight to notice;
  * `restore_mutation` is a whole-file `write_text` of a snapshot taken
    before the mutation, so two overlapping runs are last-writer-wins and
    one can PERMANENTLY PERSIST the other's mutation. Its round-trip
    assertion only proves the bytes it just wrote read back -- not that
    they are the pre-campaign bytes;
  * `apply_mutation`'s "`old` must appear exactly once" assertion is not a
    guard against any of this. Against a leftover mutation it raises
    (count 0) -- loud, but it neither detects the mutated file nor repairs
    it, and under the pytest path (one parametrized node per mutation) the
    other nine mutations run anyway, on top of the corruption.

So: every mutation is now applied inside a DISPOSABLE FILESYSTEM COPY of
the repository (`create_sandbox()` / `isolated_repo()`), and the catch
command runs with `cwd` set to that copy. The live tree is only ever READ.

WHY A FILESYSTEM COPY AND NOT `git worktree add`:
  1. this checkout is itself a LINKED WORKTREE -- `.git` here is a pointer
     file into `/Users/.../probability-arena/.git/worktrees/`. `git
     worktree add` would write new administrative state into that shared
     git directory, i.e. into another checkout's repository metadata, to
     run a test.
  2. a worktree materialises a COMMIT. The campaign must validate the
     verification layer as it exists ON DISK RIGHT NOW, including
     uncommitted work; a worktree at HEAD would silently validate
     something else.
  3. the copy is cheap -- ~8MB / ~700 files / ~0.3s -- against a campaign
     that spends ~8s in pytest subprocesses.
`.git` is DELIBERATELY EXCLUDED from the copy: a copied pointer file would
resolve to the LIVE repository, so any stray `git checkout`/`git restore`
inside the sandbox would mutate the real working tree. The sandbox is not a
git repository, on purpose.

`_write` refuses, structurally, to write anywhere under `REPO_ROOT`
(`LiveTreeWriteBlocked`): if the isolation ever regresses, the harness
fails instead of corrupting the tree. As defence in depth on top of that,
`run_campaign` and the pytest module run a `git diff --quiet -- tests/`
cleanliness check plus a sha256 census of `tests/` and `app/` both BEFORE
and AFTER, and fail loudly with the offending paths.

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

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Written into the root of every sandbox. `_sandbox_root` refuses to write
#: through any root that does not carry it, so a plain directory path (or a
#: half-built copy) can never be mistaken for a materialised sandbox.
SANDBOX_MARKER = ".meta_mutation_sandbox"

#: Where sandboxes are created. Defaults to the platform temp dir; override
#: to put them on a specific filesystem or to collect preserved ones.
SANDBOX_DIR_ENV = "META_MUTATION_SANDBOX_DIR"

#: Subtrees the live-tree tripwire censuses. `tests/` is what the catalogue
#: mutates; `app/` is included because an isolation regression that reached
#: production source would be the worst possible version of this bug.
GUARDED_SUBTREES = ("tests", "app")

_CENSUS_SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache",
                     ".ruff_cache", ".git", ".venv"}

#: `.git` and `.venv` are excluded on purpose -- see the module docstring.
#: `__pycache__` is excluded so every sandbox starts with no bytecode cache
#: (a stale `.pyc` has previously produced a false negative here).
_COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "htmlcov", ".coverage", "data",
    SANDBOX_MARKER,
)


class LiveTreeWriteBlocked(AssertionError):
    """A mutation write was aimed at (or above) the live working tree."""


class LiveTreeDirty(AssertionError):
    """The live working tree is dirty, or changed across a campaign run."""


class SandboxIntegrityError(AssertionError):
    """A sandbox is missing, or does not faithfully reproduce, a target."""


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


# ---------------------------------------------------------------------------
# Isolation: sandbox lifecycle.
# ---------------------------------------------------------------------------

def _sandbox_root(root) -> Path:
    """Validate `root` as a real sandbox and return it resolved.

    THE structural guarantee of this module. Every write goes through here,
    so "the harness mutated the live tree" is not a bug this code can have
    -- it is a raised `LiveTreeWriteBlocked`.
    """
    path = Path(root).resolve()
    if path == REPO_ROOT or REPO_ROOT in path.parents or path in REPO_ROOT.parents:
        raise LiveTreeWriteBlocked(
            f"refusing to apply a mutation at {path} -- it is the live "
            f"working tree at {REPO_ROOT} (or an ancestor/descendant of "
            "it). Mutations may only ever be written into a disposable "
            "sandbox from `create_sandbox()`/`isolated_repo()`.")
    if not (path / SANDBOX_MARKER).is_file():
        raise LiveTreeWriteBlocked(
            f"refusing to apply a mutation at {path} -- no {SANDBOX_MARKER} "
            "marker, so this is not a sandbox this module materialised.")
    return path


def create_sandbox(*, label: str = "campaign", mutations=None) -> Path:
    """Materialise a disposable copy of the repository and return its root.

    The caller owns the returned directory and MUST hand it to
    `destroy_sandbox` (or keep it deliberately, for debugging).
    `isolated_repo()` is the context-manager form and is what most callers
    should use.
    """
    parent = Path(os.environ.get(SANDBOX_DIR_ENV) or tempfile.gettempdir())
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(
        dir=parent, prefix=f"meta-mutation-{label}-{os.getpid()}-"))
    try:
        shutil.copytree(REPO_ROOT, root, symlinks=True,
                        ignore=_COPY_IGNORE, dirs_exist_ok=True)
        (root / SANDBOX_MARKER).write_text(json.dumps({
            "created_by": "tests/meta_mutation/campaign.py",
            "label": label,
            "pid": os.getpid(),
            "created_at": time.time(),
            "source_repo": str(REPO_ROOT),
        }, indent=2) + "\n")
        _assert_sandbox_is_faithful(
            root, MUTATIONS if mutations is None else mutations)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def destroy_sandbox(root) -> None:
    """Delete a sandbox. Refuses to delete anything that is not one."""
    path = _sandbox_root(root)
    shutil.rmtree(path, ignore_errors=True)


def _assert_sandbox_is_faithful(root: Path, mutations) -> None:
    """Every mutation target must exist in the sandbox with byte-identical
    content. A silently-missing or partially-copied target would turn a
    real HOLE into a spurious CAUGHT (or vice versa)."""
    bad = []
    for m in mutations:
        live = REPO_ROOT / m.target
        copied = root / m.target
        if not copied.is_file():
            bad.append(f"{m.target} (missing from the sandbox)")
        elif copied.read_bytes() != live.read_bytes():
            bad.append(f"{m.target} (bytes differ from the live tree)")
    if bad:
        raise SandboxIntegrityError(
            f"sandbox {root} does not faithfully reproduce its mutation "
            f"targets: {bad}")


@contextmanager
def isolated_repo(*, label: str = "campaign", mutations=None,
                  keep_on_error: bool = True):
    """Yield a sandbox root. Deleted on success; on failure it is LEFT IN
    PLACE and its path printed -- a preserved copy is a debugging asset,
    whereas a mutated live tree is a hazard."""
    root = create_sandbox(label=label, mutations=mutations)
    try:
        yield root
    except BaseException:
        if keep_on_error:
            print(f"meta-mutation: sandbox PRESERVED for debugging: {root}",
                  file=sys.stderr, flush=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
        raise
    else:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Defence in depth: the live-tree tripwire. Isolation can regress; this
# notices when it has, in the same run, rather than at the next `git status`.
# ---------------------------------------------------------------------------

_git_warning_emitted = False


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True)


def _git_usable() -> bool:
    """True only if `git` works AND its toplevel really is `REPO_ROOT` --
    never a repository that merely happens to contain it."""
    global _git_warning_emitted
    try:
        proc = _git("rev-parse", "--show-toplevel")
    except OSError:
        proc = None
    ok = bool(proc and proc.returncode == 0
              and Path(proc.stdout.strip() or "/").resolve()
              == Path(REPO_ROOT).resolve())
    if not ok and not _git_warning_emitted:
        _git_warning_emitted = True
        print(f"meta-mutation: WARNING -- {REPO_ROOT} is not a git working "
              "tree; falling back to the sha256 census alone for the "
              "live-tree tripwire.", file=sys.stderr, flush=True)
    return ok


def _leftover_mutation_hints(dirty: list) -> list:
    """Name any catalogue mutation that looks like it is STILL APPLIED to a
    dirty file -- the signature of an interrupted pre-isolation run."""
    hints = []
    for m in MUTATIONS:
        if m.target not in dirty:
            continue
        try:
            text = (REPO_ROOT / m.target).read_text()
        except OSError:
            continue
        if m.old not in text and (not m.new or m.new in text):
            hints.append(m.id)
    return hints


def assert_live_tree_clean(stage: str) -> None:
    """`git diff --quiet -- tests/` pre/postflight. Fails loudly, with the
    offending paths, rather than proceeding: during a mutation campaign a
    modified file under `tests/` is indistinguishable from a leftover
    mutation, and guessing wrong is how the tree got corrupted."""
    if not _git_usable():
        return
    quiet = _git("diff", "--quiet", "--", "tests/")
    if quiet.returncode == 0:
        return
    names = _git("diff", "--name-only", "--", "tests/")
    dirty = [n for n in names.stdout.splitlines() if n.strip()]
    hints = _leftover_mutation_hints(dirty)
    detail = ""
    if hints:
        detail = ("\n  these look like STILL-APPLIED catalogue mutations "
                  f"from an interrupted run: {hints}"
                  "\n  restore them (`git checkout -- <path>`) before "
                  "re-running; the campaign will not proceed over them.")
    raise LiveTreeDirty(
        f"[{stage}] `git diff --quiet -- tests/` failed: the live working "
        f"tree has uncommitted modifications under tests/: {dirty}{detail}"
        f"\n  (stderr: {quiet.stderr.strip()!r})")


def live_tree_census() -> dict:
    """sha256 of every regular file under the guarded subtrees, keyed by
    repo-relative path. A git-independent second source: it catches writes
    to ignored/untracked files that `git diff` cannot see."""
    census = {}
    for sub in GUARDED_SUBTREES:
        base = REPO_ROOT / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _CENSUS_SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                key = str(path.relative_to(REPO_ROOT))
                census[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return census


def assert_live_tree_unchanged(before: dict, stage: str) -> None:
    after = live_tree_census()
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(k for k in set(before) & set(after)
                      if before[k] != after[k])
    if added or removed or modified:
        raise LiveTreeDirty(
            f"[{stage}] the live working tree CHANGED across the campaign "
            "-- the isolation has regressed and this run wrote to "
            f"{REPO_ROOT}.\n  modified: {modified}\n  added: {added}\n  "
            f"removed: {removed}")


# ---------------------------------------------------------------------------
# Apply / run / restore. `root` is keyword-only and REQUIRED on every one of
# them: there is no default that could quietly mean "the live tree".
# ---------------------------------------------------------------------------

def _read(target: str, *, root) -> str:
    return (Path(root) / target).read_text()


def _write(target: str, text: str, *, root) -> None:
    root = _sandbox_root(root)
    (root / target).write_text(text)
    _purge_bytecode_for(root, target)


def _purge_bytecode_for(root: Path, target: str) -> None:
    """Delete the cached bytecode for exactly the file just rewritten.

    CPython invalidates a `.pyc` on source mtime AND size, so a restore
    inside the same second as its mutation is only invalidated because the
    two differ in length. That is an argument, not a guarantee -- and a
    stale `.pyc` has already produced a false negative in this harness
    once. Deleting the one cache entry that can possibly be stale removes
    the argument. Cheap and exhaustive: the target is the ONLY file whose
    bytes change during a campaign.
    """
    source = root / target
    cache = source.parent / "__pycache__"
    if not cache.is_dir():
        return
    for stale in cache.glob(f"{source.stem}.*.py[co]"):
        stale.unlink(missing_ok=True)


def apply_mutation(mutation: Mutation, *, root) -> str:
    """Apply `mutation` to its target file INSIDE THE SANDBOX `root`.
    Returns the ORIGINAL text, which the caller MUST pass to
    `restore_mutation` -- this module never keeps its own hidden backup
    registry, so a caller that discards the return value cannot
    accidentally restore from stale state."""
    root = _sandbox_root(root)
    original = _read(mutation.target, root=root)
    occurrences = original.count(mutation.old)
    if occurrences != 1:
        raise AssertionError(
            f"mutation {mutation.id!r}: expected `old` to appear exactly "
            f"once in {mutation.target}, found {occurrences} -- the target "
            "file has drifted since this mutation was written; update "
            "`old`/`new` deliberately rather than patching the wrong "
            "occurrence")
    mutated = original.replace(mutation.old, mutation.new, 1)
    _write(mutation.target, mutated, root=root)
    return original


def restore_mutation(mutation: Mutation, original_text: str, *, root) -> None:
    root = _sandbox_root(root)
    _write(mutation.target, original_text, root=root)
    restored = _read(mutation.target, root=root)
    if restored != original_text:
        raise AssertionError(
            f"mutation {mutation.id!r}: restore did not round-trip byte-"
            f"for-byte on {mutation.target} -- the SANDBOX at {root} may "
            "now differ from its pre-campaign state and needs manual "
            "review (the live tree is unaffected either way)")


def run_catch_command(mutation: Mutation, *, root, timeout_s: float = 180.0):
    """Run the mutation's named proof, with `cwd` in the sandbox.

    Every path the suite resolves is `Path(__file__)`-relative, so a pytest
    run rooted here imports `app`/`tests` from the SANDBOX and never from
    the live tree. Bytecode staleness is handled by `_purge_bytecode_for`
    on every apply and every restore rather than by disabling the cache
    wholesale, which measured ~2x slower across the campaign for no extra
    guarantee.
    """
    root = _sandbox_root(root)
    cmd = [sys.executable, "-m", "pytest", "-q", *mutation.catch_args]
    return subprocess.run(cmd, cwd=root, capture_output=True,
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


def _run_campaign_in(mutations, root: Path, verbose: bool) -> list:
    results = []
    for m in mutations:
        original = apply_mutation(m, root=root)
        try:
            proc = run_catch_command(m, root=root)
            caught = proc.returncode != 0
        finally:
            restore_mutation(m, original, root=root)
        result = {
            "id": m.id, "category": m.category, "caught": caught,
            "catch_args": m.catch_args, "returncode": proc.returncode,
        }
        results.append(result)
        if verbose:
            status = "CAUGHT" if caught else "**HOLE**"
            print(f"[{status}] {m.id} ({m.category})")
    return results


def run_campaign(mutations=MUTATIONS, *, verbose: bool = True, root=None) -> list:
    """Apply, prove-catches, restore -- one mutation at a time, never two
    mutations live at once, ALL OF IT INSIDE A DISPOSABLE COPY of the
    repository. The live working tree is only ever read.

    With `root=None` (the default) a sandbox is created for this run and
    deleted on success; if the run raises, the sandbox is left in place and
    its path printed. Pass `root=` to reuse a sandbox the caller owns.

    Concurrency-safe by construction: two runs get two `mkdtemp` sandboxes,
    so neither can observe or overwrite the other's mutations, and neither
    writes to the live tree at all -- no lock file is needed because there
    is no longer a shared resource to lock.

    Interruption-safe by construction: nothing a SIGKILL can leave behind is
    in the live tree. The worst outcome is an orphaned sandbox under
    `$META_MUTATION_SANDBOX_DIR` (or the temp dir), which is inert.
    """
    assert_live_tree_clean("preflight")
    before = live_tree_census()
    try:
        if root is None:
            with isolated_repo(label="campaign", mutations=mutations) as sandbox:
                return _run_campaign_in(mutations, sandbox, verbose)
        return _run_campaign_in(mutations, _sandbox_root(root), verbose)
    finally:
        assert_live_tree_unchanged(before, "postflight")
        assert_live_tree_clean("postflight")


if __name__ == "__main__":
    try:
        results = run_campaign()
    except LiveTreeDirty as exc:
        print(f"\nABORTED -- live-tree tripwire: {exc}", file=sys.stderr)
        sys.exit(2)
    holes = [r for r in results if not r["caught"]]
    print(f"\n{len(results)} mutations run, {len(holes)} hole(s).")
    if holes:
        for h in holes:
            print(f"  HOLE: {h['id']} -- {h['catch_args']} stayed green")
        sys.exit(1)
    sys.exit(0)
