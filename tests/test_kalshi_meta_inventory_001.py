"""KALSHI-ARCHIVE-VERIFICATION-META-001 part 1 — test the tests.

Three prior review rounds found the archive VERIFICATION HARNESSES
themselves had blind spots: an AST guard that missed 13 of 16 evasive call
shapes, a supported public argument (`verify_segment(root=None)`) that no
test ever exercised and that hangs forever on a symlink-to-FIFO, and a
`file_sha256` raw `open()` two hops below `verify_segment` that no call-graph
tool had ever traced. This module wires together the three new tools built
under `tests/meta_inventory/` to close exactly those three gaps, and reports
every production finding it surfaces along the way -- production code is
NEVER modified by anything here; see `tests/meta_inventory/__init__.py`.

A1 — call-graph inventory (`call_graph.py`): every canonical archive entry
point traced down to every filesystem/canonical/mutation/hashing/queue
primitive it reaches, committed as `tests/meta_inventory/inventory.json` and
regenerated here on every run so drift fails the suite.

A2 — red-team AST guard corpus (`red_team_corpus.py`): every evasive shape
the milestone names, in both directions (must-catch unsafe shapes,
must-not-flag known-good `evidence_fs` usage).

CONSOLIDATION (KALSHI-ARCHIVE-VERIFICATION-META-001, prerequisite to A9):
this suite used to test TWO separate guards -- the original `tests/
harness_filesystem_totality/ast_audit.py` and a strengthened `tests/
meta_inventory/ast_guard_v2.py` built to close its blind spots -- and
compare them. That was itself a hole: a mutation-campaign requirement
("mutate the guard, prove the meta-suite catches it") means NOTHING if
there are two guards and only one gets mutated, because the other stays
green. `ast_guard_v2.py` is gone; its detections are folded into `ast_audit.
py`, which is now the ONE guard this suite exercises (see that module's
docstring for the full rationale and the newly-visible-call-sites review).
The empirical "the original guard bypasses N of 22" finding is preserved
below as a FROZEN historical measurement (a hand-copied minimal reproduction
of the pre-consolidation vocabulary, not a second live guard), so the
regression this milestone fixed stays documented and provable without
resurrecting the maintenance burden of two guards.

A3 — public argument-shape matrix (`argument_matrix.py`, `matrix_runner.py`,
`parent_timeout.py`): `verify_segment`'s `root` argument crossed with
`allow_open`/`environment` against a healthy fixture (every cell must return
a bounded, typed result) and, separately, against a segment whose events
file is a symlink to a real FIFO (this is where the milestone's named defect
reproduces: `root=None` hangs; the function's own default, and every
explicit non-None root, safely refuse).
"""

from __future__ import annotations

from collections import Counter

import pytest

from tests.harness_filesystem_totality import archive_fixture as af
from tests.harness_filesystem_totality import ast_audit as guard
from tests.meta_inventory import argument_matrix as am
from tests.meta_inventory import call_graph as cg
from tests.meta_inventory import fixtures as fx
from tests.meta_inventory import matrix_runner as mr
from tests.meta_inventory import red_team_corpus as rc

TYPED_ARCHIVE_EXCEPTIONS = ("ArchiveError", "SegmentError", "ArchiveHeadError")


# =====================================================================
# A1 — production-API call graph
# =====================================================================


class TestA1CallGraphInventory:
    def test_regenerated_inventory_matches_committed_artifact(self):
        """The inventory is a committed, machine-readable artifact
        (`inventory.json`) that this test REGENERATES from the current
        source and compares byte-for-byte (as parsed JSON) against what is
        committed. A canonical call path the tool did not know about before
        -- new OR removed -- is exactly the drift this assertion exists to
        catch; per A1's acceptance criterion, silence here would mean a
        call path is invisible to the inventory.
        """
        import json
        # Round-trip through JSON before comparing: `build_inventory()`
        # returns tuples in a couple of places (`unresolved_calls` entries),
        # which is a real value under Python equality but indistinguishable
        # from a list once round-tripped through the COMMITTED artifact
        # (JSON has no tuple type). The committed file IS the artifact of
        # record, so this test compares what actually gets committed, not
        # an in-memory representation nothing else ever sees.
        regenerated = json.loads(json.dumps(cg.build_inventory()))
        committed = cg.load_committed_inventory()
        assert regenerated == committed, (
            "the call-graph inventory has drifted from the committed "
            "tests/meta_inventory/inventory.json -- regenerate it with "
            "`python3 tests/meta_inventory/call_graph.py` and review the "
            "diff before committing the new artifact")

    def test_every_entry_point_is_enumerated(self):
        inv = cg.load_committed_inventory()
        enumerated = set(inv["entry_points"])
        expected = {f"{m}::{q}" for m, q in cg.ENTRY_POINTS}
        assert enumerated == expected

    def test_verify_segment_to_file_sha256_to_raw_open_is_explicit(self):
        """A1's acceptance text: 'The known finding `verify_segment ->
        file_sha256 -> raw open()` MUST appear explicitly in your inventory
        output; if your tool does not surface it, your tool is wrong.'
        """
        inv = cg.load_committed_inventory()
        chain = cg.find_chain(inv, "verify_segment", "open")
        assert chain == list(cg.REQUIRED_CHAIN), (
            f"expected the chain {cg.REQUIRED_CHAIN!r} to be explicit in the "
            f"inventory; got {chain!r}")

    def test_classifications_are_from_the_closed_vocabulary(self):
        """FILESYSTEM ACCESS / CANONICAL ENCODING / STATE MUTATION /
        HASHING / QUEUE-ACCOUNTING — nothing else. An unexpected category
        here means the classifier itself has drifted."""
        allowed = {"FILESYSTEM ACCESS", "CANONICAL ENCODING",
                  "STATE MUTATION", "HASHING", "QUEUE-ACCOUNTING"}
        inv = cg.load_committed_inventory()
        seen = {p["classification"] for e in inv["entry_points"].values()
                for p in e["primitives"]}
        assert seen <= allowed
        # And every category is actually reached by at least one entry
        # point -- an empty category would mean the classifier lists a kind
        # of primitive nothing in this codebase's canonical paths ever hits,
        # which is worth knowing rather than assuming.
        missing = allowed - seen
        if missing:
            print(f"A1 NOTE: classification categories never reached by any "
                  f"canonical entry point: {sorted(missing)}")

    def test_every_entry_point_reaches_at_least_one_filesystem_primitive_or_is_pure(
            self):
        """Every I/O-shaped entry point must show FILESYSTEM ACCESS
        somewhere in its reachable set; a `replay`/`reconcile_with_rest`
        style pure function legitimately reaches none. Reported, not
        asserted strictly, because `EventArchive.append`/`.close` show 0
        AST-resolved primitives without the manual bridge (see
        `call_graph.MANUAL_EDGES`'s docstring) -- this test proves the
        manual bridge is actually doing that work.
        """
        inv = cg.load_committed_inventory()
        # Legitimately pure/in-memory entry points: `replay` and
        # `reconcile_with_rest` touch no disk at all by design (see their
        # own docstrings: "no network, no credential, no database"); the
        # record/manifest BUILDERS and the chain/self-digest CHECKERS
        # (`build_record`, `build_manifest`, `verify_chain`,
        # `verify_manifest_self_digest`) operate on an in-memory dict/list
        # already handed to them and never touch a path themselves -- the
        # disk read that produced their input is a DIFFERENT entry point
        # (`read_segment_records`, `SegmentWriter.close`) already covered
        # elsewhere in this inventory. `SegmentWriter.submit` similarly
        # only canonicalizes and enqueues; the writer THREAD is the one
        # that touches disk, and its close/rotation path is `SegmentWriter.
        # close`, a separate entry point that DOES show FILESYSTEM ACCESS.
        no_io_by_design = {
            "app/realtime/archive.py::replay",
            "app/realtime/archive.py::reconcile_with_rest",
            "app/realtime/archive.py::EventArchive.append",
            "app/realtime/segment.py::SegmentWriter.submit",
            "app/realtime/segment.py::build_manifest",
            "app/realtime/segment.py::build_record",
            "app/realtime/segment.py::verify_chain",
            "app/realtime/segment.py::verify_manifest_self_digest",
        }
        no_fs_access = []
        for key, entry in inv["entry_points"].items():
            classes = {p["classification"] for p in entry["primitives"]}
            if "FILESYSTEM ACCESS" not in classes and key not in no_io_by_design:
                no_fs_access.append(key)
        assert not no_fs_access, (
            f"entry points with no FILESYSTEM ACCESS primitive reachable "
            f"and not declared pure/in-memory-only: {no_fs_access} -- if "
            f"one of these NOW legitimately touches disk, remove it from "
            f"`no_io_by_design` deliberately rather than loosening this "
            f"assertion generally")
        # The two manually-bridged instance-typed edges specifically:
        append_key = "app/realtime/archive.py::EventArchive.append"
        close_key = "app/realtime/archive.py::EventArchive.close"
        assert any(b["target"].endswith("SegmentWriter.submit")
                  for b in inv["entry_points"][append_key]["manual_bridges"])
        assert any(b["target"].endswith("SegmentWriter.close")
                  for b in inv["entry_points"][close_key]["manual_bridges"])


# =====================================================================
# A2 — red-team the static/AST guard
# =====================================================================


class TestA2RedTeamGuard:
    def test_the_one_guard_discriminates_the_entire_corpus_both_directions(self):
        """Every UNSAFE shape must be flagged; every SAFE (known-good
        `evidence_fs` usage, plus two negative controls) must not be. This
        is the discrimination test the milestone requires: a guard that
        only ever says "flagged" is not a guard. Runs against `ast_audit.
        scan_source` -- the CONSOLIDATED, single guard -- not a second
        parallel module.
        """
        mismatches = []
        table = []
        for entry in rc.ALL:
            findings = guard.scan_source(entry.code)
            flagged = bool(findings)
            table.append((entry.id, entry.requirement, entry.expect_flagged,
                         flagged))
            if flagged != entry.expect_flagged:
                mismatches.append((entry.id, entry.expect_flagged, flagged))
        print("\nA2 CAUGHT/BYPASSED TABLE (the one guard):")
        for row in table:
            print(f"  {row[0]:28s} expect_flagged={row[2]!s:5s} "
                 f"got_flagged={row[3]!s:5s}  ({row[1]})")
        assert not mismatches, f"guard mismatches: {mismatches}"

    def test_pre_consolidation_vocabulary_would_have_been_measurably_weaker(self):
        """FROZEN HISTORICAL MEASUREMENT, not a second live guard: a minimal,
        hand-copied reproduction of the vocabulary `ast_audit.py` had BEFORE
        this milestone's consolidation (the four sets literally as they read
        pre-consolidation: `PATH_METHOD_NAMES` with no "open"/"read_text"/
        "stat"/"lstat", `GZIP_FUNC_NAMES = {"open"}` only, no alias tracking,
        no `getattr` dispatch, no `os.listdir`/`os.walk`/`os.path.realpath`/
        `shutil.copyfile`/`io.open`). This is deliberately NOT
        `tests/harness_filesystem_totality/ast_audit._Visitor` (that IS the
        live, consolidated, strengthened guard now) -- resurrecting a second
        live guard here would recreate the exact two-guard hole this
        consolidation exists to close. This test exists only so the
        regression this milestone fixed stays documented and empirically
        provable without paying that cost.
        """
        import ast as _ast

        old_path_methods = {"glob", "exists", "is_file", "is_dir",
                            "is_symlink", "resolve", "read_bytes", "iterdir",
                            "rglob"}
        old_os_funcs = {"open", "scandir", "stat", "lstat", "readlink"}
        old_gzip_funcs = {"open"}

        class _FrozenPreConsolidationVisitor(_ast.NodeVisitor):
            def __init__(inner):
                inner.found = False

            def visit_Call(inner, node):
                func = node.func
                if isinstance(func, _ast.Attribute):
                    if (isinstance(func.value, _ast.Name)
                            and func.value.id == "os"
                            and func.attr in old_os_funcs):
                        inner.found = True
                    elif (isinstance(func.value, _ast.Name)
                          and func.value.id == "gzip"
                          and func.attr in old_gzip_funcs):
                        inner.found = True
                    elif func.attr in old_path_methods:
                        inner.found = True
                elif isinstance(func, _ast.Name) and func.id == "open":
                    inner.found = True
                inner.generic_visit(node)

        bypassed_by_old = []
        for entry in rc.UNSAFE:
            tree = _ast.parse(entry.code)
            visitor = _FrozenPreConsolidationVisitor()
            visitor.visit(tree)
            if not visitor.found:
                bypassed_by_old.append(entry.id)
        print(f"\nA2 HISTORICAL BASELINE: the PRE-CONSOLIDATION vocabulary "
             f"would have bypassed {len(bypassed_by_old)} of {len(rc.UNSAFE)} "
             f"UNSAFE corpus entries: {bypassed_by_old}")
        assert len(bypassed_by_old) >= 10, (
            "the pre-consolidation vocabulary's weakness on this corpus "
            "should be reproducible and large; if it is now much smaller, "
            "either this frozen reproduction or the red-team corpus itself "
            "has drifted from the milestone's named shapes")
        # And the CURRENT, consolidated guard must bypass NONE of the same
        # entries -- the improvement this consolidation claims, proven on
        # the identical corpus in the identical test.
        bypassed_by_current = [e.id for e in rc.UNSAFE
                               if not guard.scan_source(e.code)]
        assert bypassed_by_current == [], (
            f"the CURRENT consolidated guard bypasses "
            f"{bypassed_by_current} -- it should bypass none of the UNSAFE "
            "corpus")

    def test_newly_visible_call_sites_are_all_reviewed_in_the_allowlist(self):
        """The five call sites the pre-consolidation vocabulary never even
        visited (four `<expr>.stat(...)`, one `gzip.GzipFile(...)`) must
        each have an explicit, hand-written allowlist entry in `ast_audit.
        ALLOWLIST` -- not merely pass the sweep silently. This is the
        machine-checkable half of "reviewed with a written justification,
        not blanket-allowlisted"; the justification TEXT itself lives as a
        comment in `ast_audit.py` and is necessarily human-reviewed, not
        asserted here.
        """
        expected_new_sites = {
            ("app/realtime/segment.py", "SegmentWriter.rotation_due", "<expr>.stat(...)"),
            ("app/realtime/segment.py", "SegmentWriter._close_stages", "<expr>.stat(...)"),
            ("app/realtime/segment.py", "verify_segment", "<expr>.stat(...)"),
            ("app/realtime/segment.py", "_abandoned_residue", "<expr>.stat(...)"),
            ("app/realtime/evidence_fs.py", "open_bounded_gzip", "gzip.GzipFile(...)"),
        }
        missing = expected_new_sites - guard.ALLOWLIST
        assert not missing, (
            f"expected these newly-visible call sites to be explicitly "
            f"allowlisted with a written justification: {missing}")
        # And the consolidated scan of production still finds NOTHING
        # outside the allowlist -- the whole point of doing the review now
        # rather than leaving the widened vocabulary to fail the fs-totality
        # harness's own `test_ast_audit_no_new_unapproved_evidence_access`.
        extra = guard.diff_against_allowlist(guard.scan_all())
        assert not extra, (
            f"consolidated guard finds call sites outside the allowlist: "
            f"{extra}")


# =====================================================================
# A3 — public argument-shape matrix
# =====================================================================


@pytest.fixture
def healthy_layout(tmp_path_factory):
    root = af.make_short_root()
    try:
        layout = mr.build_layout(root, poisoned=False)
        yield layout
    finally:
        af.teardown_root(root)


@pytest.fixture
def poisoned_layout():
    root = af.make_short_root()
    try:
        layout = mr.build_layout(root, poisoned=True)
        yield layout
    finally:
        af.teardown_root(root)
        fx.cleanup_fifo_side(layout)


class TestA3ArgumentShapeMatrix:
    def test_matrix_dimensions_and_cell_count(self):
        healthy = am.build_healthy_matrix()
        poisoned = am.build_poisoned_matrix()
        assert len(healthy) == (len(am.ROOT_MODES) * len(am.ALLOW_OPEN_VALUES)
                                * len(am.ENVIRONMENTS))
        assert len(poisoned) == len(am.ROOT_MODES)
        total = len(healthy) + len(poisoned)
        print(f"\nA3 MATRIX: {len(am.ROOT_MODES)} root_modes x "
             f"{len(am.ALLOW_OPEN_VALUES)} allow_open x "
             f"{len(am.ENVIRONMENTS)} environments = {len(healthy)} healthy "
             f"cells; + {len(poisoned)} poisoned-fixture root_mode cells; "
             f"{total} cells total")
        assert total == 28

    def test_every_healthy_cell_is_bounded_and_never_a_raw_builtin_exception(
            self, healthy_layout):
        """Every SUPPORTED shape against a genuinely valid segment must
        return a bounded, typed result: `RETURNED` (a `SegmentVerdict`) or
        `RAISED` with a TYPED archive exception. `TIMEOUT`, `CRASHED`,
        `PROTOCOL_VIOLATION`, or a raw builtin exception type (`TypeError`,
        `AttributeError`, `KeyError`, ...) all fail this test.
        """
        classifications = Counter()
        hung_or_crashed = []
        untyped_raises = []
        for cell in am.build_healthy_matrix():
            outcome = mr.run_cell(cell, healthy_layout)
            classifications[outcome["classification"]] += 1
            if outcome["classification"] in ("TIMEOUT", "CRASHED",
                                             "PROTOCOL_VIOLATION"):
                hung_or_crashed.append((cell["label"], outcome))
            if (outcome["classification"] == "RAISED"
                    and outcome.get("exception_type") not in TYPED_ARCHIVE_EXCEPTIONS):
                untyped_raises.append((cell["label"], outcome.get("exception_type"),
                                      outcome.get("exception_message")))
        print(f"\nA3 HEALTHY-FIXTURE MATRIX: {dict(classifications)}")
        assert not hung_or_crashed, f"unbounded healthy-fixture cells: {hung_or_crashed}"
        assert not untyped_raises, f"raw builtin exceptions escaped: {untyped_raises}"

    def test_root_none_on_symlinked_fifo_hangs_while_every_other_root_shape_refuses(
            self, poisoned_layout):
        """THE finding this milestone names explicitly: `verify_segment`'s
        default (`root` omitted, deriving `_DERIVE_ROOT`) and every
        EXPLICIT non-None root safely refuse a symlink-to-FIFO events path
        (the containment check's `is_symlink()` component rejection fires
        before any `open()`). `root=None` skips that ENTIRE containment
        block, reaches `file_sha256`'s raw `open()`, and hangs.

        This is asserted, not merely printed, because "root=None is
        documented and supported and hangs" is a production finding this
        milestone exists to prove empirically — and REPORT, not fix (no
        edit to `app/realtime/segment.py` happens anywhere in this repo
        change).
        """
        outcomes = {}
        for cell in am.build_poisoned_matrix():
            outcomes[cell["root_mode"]] = mr.run_cell(cell, poisoned_layout)

        print("\nA3 POISONED-FIXTURE (symlink-to-FIFO events path) MATRIX:")
        for mode, outcome in outcomes.items():
            print(f"  root={mode:18s} -> {outcome['classification']:10s} "
                 f"{outcome.get('detail') or outcome.get('exception_type') or ''}")

        for mode in ("omit", "explicit_correct", "explicit_wrong"):
            outcome = outcomes[mode]
            assert outcome["classification"] == "RETURNED", (
                f"root={mode!r} was expected to safely refuse the "
                f"symlink-to-FIFO segment, not {outcome['classification']}: "
                f"{outcome}")
            assert outcome["detail"]["valid"] is False

        none_outcome = outcomes["none"]
        assert none_outcome["classification"] == "TIMEOUT", (
            "EXPECTED FINDING: verify_segment(..., root=None) against a "
            "symlink-to-FIFO events path should hang past the parent-"
            f"enforced timeout. Got {none_outcome['classification']!r} "
            f"instead: {none_outcome}. If this now returns/raises instead "
            "of hanging, the underlying defect may have been fixed "
            "elsewhere -- update this test's expectation deliberately, "
            "do not silently loosen it.")

    def test_driver_reports_typed_failures_not_silence(self, tmp_path):
        """A DIRECT unit test of `matrix_cell_driver.py`'s own exception
        reporting fidelity, independent of whether any current matrix cell
        happens to make `verify_segment` raise (today, none reliably do --
        it returns typed verdicts, not exceptions, for every argument shape
        this matrix explores). Without this test, A9's mutation campaign
        found that swallowing the driver's top-level `except BaseException`
        left the ENTIRE A3 suite green (a HOLE): a driver that never reports
        `raised=True` turns a genuine exception into a silent, bogus
        `RETURNED` with `detail=None`, invisible to every existing
        hung/crashed/untyped-raise check because none of those cells are
        the ones a real fix would ever route an exception through anyway.
        Drives the driver directly with a deliberately unknown `api` value
        (`_call` raises `ValueError(f"unknown api {api!r}")` for it,
        unconditionally, by construction) and asserts the JSON payload
        faithfully reports it.
        """
        import json
        import subprocess
        import sys

        from tests.meta_inventory.matrix_runner import _DRIVER

        args = {"api": "definitely-not-a-real-api-shape", "kwargs": {}}
        proc = subprocess.run(
            [sys.executable, str(_DRIVER), json.dumps(args)],
            capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, (
            "the driver's own top-level guard must never let an unhandled "
            f"exception escape as a nonzero exit: {proc.stderr}")
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(lines) == 1, f"expected exactly one JSON line: {proc.stdout!r}"
        payload = json.loads(lines[0])
        assert payload["raised"] is True, (
            f"expected the driver to report raised=True for an unknown api "
            f"shape; got {payload}")
        assert payload["exception_type"] == "ValueError", payload
