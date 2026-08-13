"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 -- proofs that the mutation campaign
cannot corrupt the tree it is verifying.

This harness has already caused the failure it exists to prevent: an
orphaned reviewer process left `tests/meta_runtime/aggregate_work.py`
mutated in the live working tree, restore never having run. The fix is
isolation -- `tests/meta_mutation/campaign.py` mutates a disposable copy of
the repository and only ever READS the live tree -- and the properties
below are the evidence for it, not an assertion that it should hold.

WHY THE KILL PROOFS RUN AGAINST A PRISTINE COPY RATHER THAN THIS CHECKOUT.
Each proof stands up its own complete copy of the repository and treats
THAT as the working tree the campaign is launched from: `campaign.py`
derives `REPO_ROOT` from `__file__`, so a campaign started from
`<copy>/tests/meta_mutation/campaign.py` treats `<copy>` as the live tree
in every respect. That buys determinism a test against this checkout cannot
have -- a guaranteed-clean starting state, an exact byte census with no
interference from whatever else is uncommitted here, and no SIGKILLed
process ever aimed at a real repository. The property proven is identical,
and every proof ALSO censuses this checkout (see `_this_checkout_unchanged`)
so an escape would still be caught here.

The `git`-backed tripwire is proven separately, against a purpose-built
throwaway git repository, in `TestLiveTreeTripwire`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.meta_mutation import campaign as camp

#: Two mutations that target the SAME file. Concurrency proofs use these so
#: the two runs collide on one path if isolation is not real.
_COLLIDING_IDS = ("M5_independent_accounting_made_tautological",
                  "M10_durable_disposition_reads_the_tautological_source")

_KILL_DEADLINE_S = 120.0

#: Captured at import, before any test can monkeypatch `camp.REPO_ROOT`.
#: The postflight below must always mean THIS checkout, whatever a test has
#: pointed the module's own constant at.
_THIS_CHECKOUT = camp.REPO_ROOT


def _census(root: Path) -> dict:
    """sha256 of every regular file under a tree, minus caches. The same
    shape as `camp.live_tree_census()` but for an arbitrary root, so a
    pristine copy can be compared exactly the way the live tree is."""
    out = {}
    for sub in camp.GUARDED_SUBTREES:
        base = root / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in camp._CENSUS_SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_symlink() or not path.is_file():
                    continue
                out[str(path.relative_to(root))] = hashlib.sha256(
                    path.read_bytes()).hexdigest()
    return out


def _diff(before: dict, after: dict) -> dict:
    return {
        "modified": sorted(k for k in set(before) & set(after)
                           if before[k] != after[k]),
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
    }


@pytest.fixture(autouse=True)
def _this_checkout_unchanged():
    """Every test in this module leaves THIS checkout byte-identical.

    Equality only -- deliberately not `assert_live_tree_clean`, which
    refuses to run over a dirty `tests/`. These proofs must be runnable
    while the harness itself is being edited; the cleanliness preflight is
    enforced where the mutations actually happen
    (`test_kalshi_meta_mutation_campaign_001.py`).
    """
    before = _census(_THIS_CHECKOUT)
    assert before, "the live-tree census found no files -- it is not looking"
    yield
    assert _diff(before, _census(_THIS_CHECKOUT)) == {
        "modified": [], "added": [], "removed": []}, (
        f"this checkout at {_THIS_CHECKOUT} changed across the test")


@pytest.fixture
def pristine_repo(tmp_path) -> Path:
    """A complete, clean copy of the repository, standing in for the live
    working tree a campaign is launched from."""
    root = tmp_path / "pristine-repo"
    shutil.copytree(camp.REPO_ROOT, root, symlinks=True,
                    ignore=camp._COPY_IGNORE)
    assert (root / "tests" / "meta_mutation" / "campaign.py").is_file()
    return root


def _launch_campaign(repo: Path, sandbox_dir: Path) -> subprocess.Popen:
    """Start the standalone campaign against `repo`, in its own process
    group so a proof can signal the parent AND its pytest child together --
    which is what a killed terminal actually does."""
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **{camp.SANDBOX_DIR_ENV: str(sandbox_dir)})
    return subprocess.Popen(
        [sys.executable, "tests/meta_mutation/campaign.py"],
        cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True)


def _wait_until_a_mutation_is_live(repo: Path, sandbox_dir: Path,
                                   baseline: dict, proc) -> tuple:
    """Block until some catalogue target is observed MUTATED somewhere, and
    report where: ``("sandbox", path)`` or ``("repo", path)``.

    Returning ``"repo"`` is the pre-isolation behaviour and is exactly what
    a revert of this change produces -- the caller asserts on the tree
    afterwards, so the proof fails loudly instead of hanging.
    """
    deadline = time.monotonic() + _KILL_DEADLINE_S
    while time.monotonic() < deadline:
        for m in camp.MUTATIONS:
            live = repo / m.target
            if live.is_file() and hashlib.sha256(
                    live.read_bytes()).hexdigest() != baseline[m.target]:
                return "repo", live
        for candidate in sorted(sandbox_dir.glob("meta-mutation-*")):
            for m in camp.MUTATIONS:
                copied = candidate / m.target
                if copied.is_file() and hashlib.sha256(
                        copied.read_bytes()).hexdigest() != baseline[m.target]:
                    return "sandbox", copied
        if proc.poll() is not None:
            raise AssertionError(
                "the campaign exited before any mutation was observed "
                f"applied (returncode={proc.returncode}); stderr:\n"
                f"{proc.stderr.read()[-3000:]}")
        time.sleep(0.02)
    raise AssertionError(
        f"no mutation was observed applied anywhere within "
        f"{_KILL_DEADLINE_S}s -- the proof could not reach the state it "
        "exists to interrupt")


def _kill_group(proc, sig) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except ProcessLookupError:
        pass


# =====================================================================
# The sandbox itself.
# =====================================================================

class TestSandboxShape:

    def test_a_sandbox_reproduces_every_mutation_target_byte_for_byte(self):
        with camp.isolated_repo(label="shape") as root:
            for m in camp.MUTATIONS:
                assert (root / m.target).read_bytes() == (
                    camp.REPO_ROOT / m.target).read_bytes(), (
                    f"{m.target} was not copied faithfully")

    def test_a_sandbox_is_a_runnable_checkout_without_git_or_venv(self):
        with camp.isolated_repo(label="nogit") as root:
            assert not (root / ".git").exists(), (
                "the sandbox carries a .git that resolves to the live repo")
            assert not (root / ".venv").exists()
            # ...but it IS a runnable checkout.
            assert (root / "pytest.ini").is_file()
            assert (root / "app").is_dir()
            assert (root / "tests" / "conftest.py").is_file()

    def test_git_and_venv_are_excluded_even_when_the_source_has_them(
            self, tmp_path, monkeypatch):
        """`.git` in this checkout is a POINTER FILE into another
        checkout's shared git directory. Copying it would make the sandbox
        resolve to the LIVE repository, so a stray `git checkout`/`git
        restore` inside the sandbox would mutate the real working tree --
        the exact accident a prior agent already had with `git checkout --`.

        Stands up a source tree that definitely HAS a `.git` pointer and a
        `.venv` symlink, rather than asserting over this checkout: a copy
        of this repo made without `.git` (which is how the sandbox and
        every scratch copy are made) would pass the weaker check for free.
        """
        source = tmp_path / "source-repo"
        (source / "tests").mkdir(parents=True)
        (source / "tests" / "keep.py").write_text("keep = 1\n")
        (source / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x\n")
        (source / ".venv").symlink_to(tmp_path, target_is_directory=True)
        monkeypatch.setattr(camp, "REPO_ROOT", source)
        monkeypatch.setenv(camp.SANDBOX_DIR_ENV, str(tmp_path / "sandboxes"))

        with camp.isolated_repo(label="nogit", mutations=()) as root:
            assert not (root / ".git").exists(), (
                "the sandbox carries a .git pointing at the live repository")
            assert not (root / ".venv").exists()
            assert (root / "tests" / "keep.py").read_text() == "keep = 1\n"

    def test_a_sandbox_starts_with_no_bytecode_cache(self):
        """A stale `.pyc` has produced a false negative in this harness
        before; a fresh sandbox must carry none."""
        with camp.isolated_repo(label="nopyc") as root:
            assert not list(root.rglob("__pycache__"))
            assert not list(root.rglob("*.pyc"))

    def test_applying_and_restoring_purge_the_target_bytecode(self):
        """The second half of the `.pyc` guarantee: within a sandbox the
        cache does get written, so both the apply and the restore must drop
        the target's cache entry -- CPython's mtime+size invalidation would
        otherwise be the only thing standing between a same-second restore
        and a stale module."""
        m = camp.MUTATIONS[0]
        with camp.isolated_repo(label="pyc") as root:
            cache = (root / m.target).parent / "__pycache__"
            cache.mkdir(exist_ok=True)
            stem = Path(m.target).stem
            planted = cache / f"{stem}.cpython-999.pyc"

            planted.write_bytes(b"stale-from-before-the-mutation")
            original = camp.apply_mutation(m, root=root)
            assert not planted.exists(), "apply left a stale .pyc in place"

            planted.write_bytes(b"stale-from-the-mutated-source")
            camp.restore_mutation(m, original, root=root)
            assert not planted.exists(), "restore left a stale .pyc in place"

    def test_the_marker_records_its_provenance(self):
        with camp.isolated_repo(label="marker") as root:
            meta = json.loads((root / camp.SANDBOX_MARKER).read_text())
            assert meta["source_repo"] == str(camp.REPO_ROOT)
            assert meta["pid"] == os.getpid()

    def test_a_sandbox_missing_a_target_is_rejected_and_not_leaked(
            self, tmp_path, monkeypatch):
        monkeypatch.setenv(camp.SANDBOX_DIR_ENV, str(tmp_path))
        bogus = dataclasses.replace(
            camp.MUTATIONS[0], target="tests/this_file_does_not_exist.py")
        with pytest.raises(camp.SandboxIntegrityError):
            camp.create_sandbox(label="bad", mutations=(bogus,))
        assert not list(tmp_path.glob("meta-mutation-*")), (
            "a sandbox that failed its fidelity check was left behind")


class TestSandboxLifecycle:

    def test_a_sandbox_is_deleted_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv(camp.SANDBOX_DIR_ENV, str(tmp_path))
        with camp.isolated_repo(label="ok") as root:
            assert root.is_dir()
        assert not root.exists()
        assert not list(tmp_path.glob("meta-mutation-*"))

    def test_a_sandbox_is_preserved_and_its_path_printed_on_failure(
            self, tmp_path, monkeypatch, capsys):
        """A preserved copy is a debugging asset; a mutated live tree is a
        hazard. Failure must keep the former."""
        monkeypatch.setenv(camp.SANDBOX_DIR_ENV, str(tmp_path))
        with pytest.raises(RuntimeError):
            with camp.isolated_repo(label="boom") as root:
                (root / "tests" / "meta_mutation" / "campaign.py").write_text(
                    "# scribbled on\n")
                raise RuntimeError("deliberate")
        assert root.is_dir(), "the sandbox was deleted on failure"
        assert (root / "tests" / "meta_mutation" / "campaign.py").read_text() \
            == "# scribbled on\n", "the preserved sandbox lost its evidence"
        assert str(root) in capsys.readouterr().err

    def test_concurrent_sandboxes_are_distinct_directories(self):
        with camp.isolated_repo(label="a") as a, camp.isolated_repo(label="b") as b:
            assert a != b
            target = camp.MUTATIONS[0].target
            (a / target).write_text("A")
            assert (b / target).read_text() != "A"


# =====================================================================
# The structural write guard.
# =====================================================================

class TestWritesCannotReachTheLiveTree:

    @pytest.mark.parametrize("bad", ["repo_root", "subdir", "parent"])
    def test_write_is_blocked_at_or_around_the_live_tree(self, bad):
        root = {"repo_root": camp.REPO_ROOT,
                "subdir": camp.REPO_ROOT / "tests" / "meta_mutation",
                "parent": camp.REPO_ROOT.parent}[bad]
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp._write("tests/meta_mutation/campaign.py", "x", root=root)

    def test_a_sandbox_marker_does_not_authorise_the_live_tree(
            self, tmp_path, monkeypatch):
        """The ancestry check and the marker check are INDEPENDENT guards,
        and this proves the ancestry one on its own.

        Without this, a marker planted (or left by an earlier run rooted
        elsewhere) inside the live tree would be the only thing standing
        between the harness and the repository -- and the marker check
        alone would still pass the weaker "REPO_ROOT is rejected" test,
        because the live tree happens not to contain a marker.
        """
        source = tmp_path / "repo"
        (source / "tests").mkdir(parents=True)
        monkeypatch.setattr(camp, "REPO_ROOT", source)
        for candidate in (source, source / "tests", tmp_path):
            (candidate / camp.SANDBOX_MARKER).write_text("{}\n")
            with pytest.raises(camp.LiveTreeWriteBlocked):
                camp._sandbox_root(candidate)

    def test_an_unmarked_directory_is_not_a_sandbox(self, tmp_path):
        shutil.copytree(camp.REPO_ROOT, tmp_path / "copy", symlinks=True,
                        ignore=camp._COPY_IGNORE)
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp._sandbox_root(tmp_path / "copy")

    def test_destroy_sandbox_refuses_anything_that_is_not_a_sandbox(
            self, tmp_path):
        (tmp_path / "keep.txt").write_text("precious")
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp.destroy_sandbox(tmp_path)
        with pytest.raises(camp.LiveTreeWriteBlocked):
            camp.destroy_sandbox(camp.REPO_ROOT)
        assert (tmp_path / "keep.txt").read_text() == "precious"


# =====================================================================
# The defence-in-depth tripwire.
# =====================================================================

@pytest.fixture
def throwaway_git_repo(tmp_path) -> Path:
    repo = tmp_path / "gitrepo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "x.py").write_text("ALPHA = 1\n")
    (repo / "tests" / "y.py").write_text("y = 1\n")
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True,
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-qm", "seed")
    return repo


class TestLiveTreeTripwire:

    def test_a_clean_tree_passes(self, throwaway_git_repo, monkeypatch):
        monkeypatch.setattr(camp, "REPO_ROOT", throwaway_git_repo)
        camp.assert_live_tree_clean("unit")

    def test_a_dirty_tests_tree_fails_loudly_naming_the_paths(
            self, throwaway_git_repo, monkeypatch):
        monkeypatch.setattr(camp, "REPO_ROOT", throwaway_git_repo)
        (throwaway_git_repo / "tests" / "y.py").write_text("y = 2\n")
        with pytest.raises(camp.LiveTreeDirty) as exc:
            camp.assert_live_tree_clean("preflight")
        assert "tests/y.py" in str(exc.value)
        assert "preflight" in str(exc.value)

    def test_a_leftover_mutation_is_named_as_such(
            self, throwaway_git_repo, monkeypatch):
        """The precise failure that started this: a still-applied mutation
        from an interrupted run. The tripwire must say so, not merely
        `file modified`."""
        monkeypatch.setattr(camp, "REPO_ROOT", throwaway_git_repo)
        fake = dataclasses.replace(
            camp.MUTATIONS[0], id="FAKE_leftover", target="tests/x.py",
            old="ALPHA = 1", new="BETA = 1")
        monkeypatch.setattr(camp, "MUTATIONS", (fake,))
        (throwaway_git_repo / "tests" / "x.py").write_text("BETA = 1\n")
        with pytest.raises(camp.LiveTreeDirty) as exc:
            camp.assert_live_tree_clean("preflight")
        assert "FAKE_leftover" in str(exc.value)
        assert "STILL-APPLIED" in str(exc.value)

    def test_the_census_catches_a_change_git_diff_would_not_see(
            self, throwaway_git_repo, monkeypatch):
        """Second, git-independent source: a write to an untracked or
        ignored path is invisible to `git diff` but not to the census."""
        monkeypatch.setattr(camp, "REPO_ROOT", throwaway_git_repo)
        before = camp.live_tree_census()
        (throwaway_git_repo / "tests" / "untracked.py").write_text("z = 1\n")
        camp.assert_live_tree_clean("still-clean-per-git")
        with pytest.raises(camp.LiveTreeDirty) as exc:
            camp.assert_live_tree_unchanged(before, "postflight")
        assert "tests/untracked.py" in str(exc.value)

    def test_run_campaign_refuses_to_start_over_a_dirty_tests_tree(
            self, throwaway_git_repo, monkeypatch):
        monkeypatch.setattr(camp, "REPO_ROOT", throwaway_git_repo)
        (throwaway_git_repo / "tests" / "y.py").write_text("y = 3\n")
        with pytest.raises(camp.LiveTreeDirty):
            camp.run_campaign((), verbose=False)


# =====================================================================
# Interruption. No `finally` survives SIGKILL -- so nothing a SIGKILL can
# interrupt is allowed to be in the live tree in the first place.
# =====================================================================

class TestInterruptionLeavesTheTreeIntact:

    def test_normal_completion_leaves_the_tree_byte_identical(
            self, pristine_repo, tmp_path):
        before = _census(pristine_repo)
        proc = _launch_campaign(pristine_repo, tmp_path / "sandboxes")
        out, err = proc.communicate(timeout=600)
        assert proc.returncode == 0, f"stdout:\n{out}\nstderr:\n{err}"
        assert out.count("[CAUGHT]") == len(camp.MUTATIONS)
        assert _diff(before, _census(pristine_repo)) == {
            "modified": [], "added": [], "removed": []}
        assert not list((tmp_path / "sandboxes").glob("meta-mutation-*")), (
            "a successful campaign left its sandbox behind")

    @pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM],
                             ids=["SIGINT", "SIGTERM"])
    def test_a_signalled_campaign_leaves_the_tree_byte_identical(
            self, pristine_repo, tmp_path, sig):
        before = _census(pristine_repo)
        sandbox_dir = tmp_path / "sandboxes"
        proc = _launch_campaign(pristine_repo, sandbox_dir)
        where, path = _wait_until_a_mutation_is_live(
            pristine_repo, sandbox_dir, before, proc)
        assert where == "sandbox", (
            f"a mutation was applied to the LIVE TREE at {path} -- the "
            "campaign is not isolated")
        _kill_group(proc, sig)
        proc.communicate(timeout=120)
        assert _diff(before, _census(pristine_repo)) == {
            "modified": [], "added": [], "removed": []}

    def test_a_SIGKILLed_campaign_leaves_the_tree_byte_identical(
            self, pristine_repo, tmp_path):
        """THE case the previous design could not survive. SIGKILL runs no
        `finally`, no `atexit`, no signal handler -- the whole restore
        mechanism is skipped. The tree survives only because the mutation
        was never in it. The orphaned sandbox is the acceptable residue.
        """
        before = _census(pristine_repo)
        sandbox_dir = tmp_path / "sandboxes"
        proc = _launch_campaign(pristine_repo, sandbox_dir)
        where, mutated_path = _wait_until_a_mutation_is_live(
            pristine_repo, sandbox_dir, before, proc)
        assert where == "sandbox", (
            f"a mutation was applied to the LIVE TREE at {mutated_path} -- "
            "the campaign is not isolated and SIGKILL will strand it there")
        _kill_group(proc, signal.SIGKILL)
        proc.communicate(timeout=120)
        assert proc.returncode != 0

        assert _diff(before, _census(pristine_repo)) == {
            "modified": [], "added": [], "removed": []}, (
            "SIGKILL left the working tree modified")
        # The mutation really was live and unrestored at the moment of the
        # kill -- otherwise this proves nothing about interruption.
        assert mutated_path.exists()
        surviving = sorted(sandbox_dir.glob("meta-mutation-*"))
        assert surviving, (
            "no sandbox survived the kill -- the interruption did not "
            "happen where this proof needs it to")

    def test_an_interrupted_run_does_not_block_the_next_one(
            self, pristine_repo, tmp_path):
        """Recovery, not just survival: after a SIGKILL the tree is clean,
        so a fresh campaign runs to completion with no manual repair. Under
        the previous design the leftover mutation made `apply_mutation`
        raise on that target forever."""
        before = _census(pristine_repo)
        sandbox_dir = tmp_path / "sandboxes"
        first = _launch_campaign(pristine_repo, sandbox_dir)
        _wait_until_a_mutation_is_live(
            pristine_repo, sandbox_dir, before, first)
        _kill_group(first, signal.SIGKILL)
        first.communicate(timeout=120)

        second = _launch_campaign(pristine_repo, tmp_path / "sandboxes2")
        out, err = second.communicate(timeout=600)
        assert second.returncode == 0, f"stdout:\n{out}\nstderr:\n{err}"
        assert out.count("[CAUGHT]") == len(camp.MUTATIONS)
        assert _diff(before, _census(pristine_repo)) == {
            "modified": [], "added": [], "removed": []}


# =====================================================================
# Concurrency. The pre-isolation design was WORSE than unguarded here:
# both runs snapshot `original` before either writes, and `restore_mutation`
# is a whole-file write of that stale snapshot, so the loser can
# permanently persist the winner's mutation.
# =====================================================================

_CONCURRENT_DRIVER = """
import json, sys
sys.path.insert(0, {repo!r})
from tests.meta_mutation import campaign as camp
subset = tuple(m for m in camp.MUTATIONS if m.id in {ids!r})
assert len(subset) == len({ids!r})
results = camp.run_campaign(subset, verbose=False)
print("RESULTS " + json.dumps(
    [{{"id": r["id"], "caught": r["caught"]}} for r in results]))
"""


class TestConcurrentRunsDoNotInterfere:

    def test_two_concurrent_campaigns_on_the_same_target_file(
            self, pristine_repo, tmp_path):
        """Both runs mutate the SAME file (M5 and M10 both target
        `tests/meta_runtime/independent_accounting.py`). Each must reach
        its own verdict and leave the tree byte-identical."""
        before = _census(pristine_repo)
        code = _CONCURRENT_DRIVER.format(
            repo=str(pristine_repo), ids=_COLLIDING_IDS)
        procs = []
        for i in range(2):
            env = dict(os.environ, **{
                camp.SANDBOX_DIR_ENV: str(tmp_path / f"sandboxes{i}")})
            (tmp_path / f"sandboxes{i}").mkdir()
            procs.append(subprocess.Popen(
                [sys.executable, "-c", code], cwd=pristine_repo, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        outputs = [p.communicate(timeout=900) for p in procs]

        for i, (proc, (out, err)) in enumerate(zip(procs, outputs)):
            assert proc.returncode == 0, (
                f"concurrent run {i} failed\nstdout:\n{out}\nstderr:\n{err}")
            line = [ln for ln in out.splitlines() if ln.startswith("RESULTS ")]
            assert line, f"run {i} produced no verdict:\n{out}\n{err}"
            verdicts = json.loads(line[0][len("RESULTS "):])
            assert sorted(v["id"] for v in verdicts) == sorted(_COLLIDING_IDS)
            assert all(v["caught"] for v in verdicts), (
                f"run {i} lost a detection under concurrency: {verdicts}")

        assert _diff(before, _census(pristine_repo)) == {
            "modified": [], "added": [], "removed": []}, (
            "two concurrent campaigns modified the working tree")
        for i in range(2):
            assert not list((tmp_path / f"sandboxes{i}").glob("meta-mutation-*"))


# =====================================================================
# The catalogue is unchanged by this work. Isolation, not redesign.
# =====================================================================

def test_the_catalogue_is_unchanged_in_size_shape_and_targets():
    assert len(camp.MUTATIONS) == 10
    assert [m.id for m in camp.MUTATIONS] == [
        "M1_guard_drop_api_name",
        "M2_call_graph_drop_entry_point",
        "M3_parent_timeout_weakened",
        "M4_aggregate_work_bound_vacuous",
        "M5_independent_accounting_made_tautological",
        "M6_duplicate_key_check_removed",
        "M7_argument_matrix_drops_root_none",
        "M8_matrix_driver_swallows_typed_failure",
        "M9_strict_xfail_forced_always",
        "M10_durable_disposition_reads_the_tautological_source",
    ]
    for m in camp.MUTATIONS:
        assert m.target.startswith("tests/")
        assert (camp.REPO_ROOT / m.target).is_file()
        assert (camp.REPO_ROOT / m.target).read_text().count(m.old) == 1, (
            f"{m.id}: its `old` anchor is no longer unique in {m.target}")
