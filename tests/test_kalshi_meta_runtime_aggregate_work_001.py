"""KALSHI-ARCHIVE-VERIFICATION-META-001 A5 -- the aggregate-work property,
measured against production, and discriminated against planted encoders.

DOES NOT MODIFY app/realtime/canonical.py or app/realtime/segment.py. See
`tests/meta_runtime/aggregate_work.py` for the property statement in full
and why it is not satisfied by production's `CapabilityLimits` today.

Structure:
  Section 1 -- the generator's own shape is proven legal (small object
               count, legal depth, legal local width) so nobody can dismiss
               the finding as "that input should have been refused anyway".
  Section 2 -- PRODUCTION's real `canonical_bytes` measured directly:
               bytes emitted grows exponentially in depth for a LINEAR
               growth in object count -- the property VIOLATED, pinned as a
               known, currently-true reproduction (not xfail: the defect is
               deterministic and always fires, same convention as
               `test_kalshi_encoder_fidelity_harness_001.py`'s "known
               defect" classes).
  Section 3 -- the admission walk hangs outright at the reviewer's own
               depth (61), proven via the parent-supervised subprocess
               timeout helper (A4) so this file cannot itself hang.
  Section 4 -- discrimination: `BadEncoderNoAggregateBound` (the class of
               omission production has) is caught by the SAME property
               check; `GoodEncoderWithAggregateBound` (a declared,
               enforced total-work ceiling) satisfies it. Proves the
               methodology is not vacuous either way.

IF PRODUCTION IS FIXED (an aggregate bound is added to `CapabilityLimits`
and enforced by both the admission walk and the encoder): Section 2's
byte-growth assertions and Section 3's TIMEOUT_FAIL assertion will both
start FAILING LOUDLY (byte growth will flatten to a bounded refusal, and the
depth-61 admission call will return quickly with a `CanonicalError` /
`non_canonical_reason` result instead of hanging) -- that failure is the
signal to update this file to assert the new, bounded-refusal behaviour
instead of the current hang/exponential-growth behaviour. This is the
"known-defect ledger" pattern this milestone's brief asks for, applied
without `pytest.mark.xfail` because a subprocess-timeout-based reproduction
does not fit that decorator's pass/fail shape cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.meta_runtime.aggregate_work import (
    AggregateWorkExceeded,
    BadEncoderNoAggregateBound,
    GoodEncoderWithAggregateBound,
    count_distinct_objects,
    make_shared_dag,
    measure_via_canonical_bytes,
)
from tests.meta_runtime.parent_timeout import run_with_parent_timeout

REPO_ROOT = str(Path(__file__).resolve().parents[1])


# =====================================================================
# 1. THE GENERATOR IS LEGAL BY EVERY LOCAL RULE CapabilityLimits DECLARES.
# =====================================================================

class TestGeneratorShapeIsLegal:
    @pytest.mark.parametrize("depth", [5, 10, 15, 20])
    def test_object_count_grows_linearly_not_exponentially(self, depth):
        x = make_shared_dag(depth)
        n = count_distinct_objects(x)
        assert n == depth + 1, (
            "the generator is supposed to SHARE, not duplicate -- a "
            f"linear object count is the whole point; got {n} at depth "
            f"{depth}")

    @pytest.mark.parametrize("depth", [5, 20, 60, 120])
    def test_depth_is_legal_under_MAX_DEPTH(self, depth):
        assert depth <= cn.CapabilityLimits.MAX_DEPTH, (
            "recalibrate the test depths -- this suite's own scenarios "
            "must stay inside the declared capability, or a refusal here "
            "would be entirely correct and prove nothing about aggregate "
            "work")

    def test_local_container_width_is_legal(self):
        # Every container `make_shared_dag` builds has exactly 2 elements.
        assert 2 <= cn.CapabilityLimits.MAX_SEQUENCE_ELEMENTS

    def test_admission_admits_the_generator_at_a_moderate_depth(self):
        """Confirms this is not a structurally-refused shape at ordinary
        depth -- the property gap is specifically about what happens once
        depth gets large, not about the shape being illegal outright."""
        x = make_shared_dag(15)
        assert sg.non_canonical_reason(x) is None


# =====================================================================
# 2. PRODUCTION'S canonical_bytes MEASURED DIRECTLY: byte output grows
#    EXPONENTIALLY in depth despite a LINEAR object count. This is the
#    property violation, pinned as a deterministic, currently-true
#    reproduction.
# =====================================================================

class TestAggregateWorkPropertyViolatedByProduction:
    """KNOWN DEFECT, pinned (not xfail -- deterministic, always reproduces
    at these depths). `CapabilityLimits` has no `MAX_TOTAL_NODES` and
    neither `_structural_reason` nor `_encode` tracks a running total
    across the call, so nothing refuses this until the process itself
    cannot keep up (Section 3)."""

    def test_bytes_emitted_doubles_per_extra_depth_level(self):
        measurements = [
            measure_via_canonical_bytes(d, canonical_bytes_fn=cn.canonical_bytes)
            for d in (10, 12, 14, 16, 18)
        ]
        for m in measurements:
            assert m.raised is None, (
                f"depth {m.depth}: canonical_bytes refused "
                f"({m.raised}) -- if this is a NEW aggregate bound, "
                "update this file: the property now HOLDS and the "
                "assertions below should change to assert a bounded "
                "refusal instead of measuring unbounded growth")
        # Adjacent-depth ratio should be close to 2x (one more level of
        # sharing doubles the leaf-path count) -- nowhere near the ~1x
        # (or slightly super-linear, for JSON's fixed per-key overhead)
        # ratio a genuinely OBJECT-COUNT-bounded encoder would show.
        ratios = [
            measurements[i + 1].bytes_emitted / measurements[i].bytes_emitted
            for i in range(len(measurements) - 1)
        ]
        assert all(r > 1.7 for r in ratios), (
            f"expected roughly-doubling byte growth per extra depth level "
            f"(the AGGREGATE-WORK property being violated); got ratios "
            f"{ratios} over {[(m.depth, m.bytes_emitted) for m in measurements]}"
            " -- if this no longer doubles, production may have gained an "
            "aggregate bound; treat that as a defect FIX to verify, not a "
            "harness bug to silence")
        # And the object count behind those measurements stayed linear the
        # whole time -- the mismatch between "small, linear cause" and
        # "exponential, unbounded effect" IS the property violation.
        obj_counts = [m.n_distinct_objects for m in measurements]
        assert obj_counts == [d + 1 for d in (10, 12, 14, 16, 18)]

    def test_work_is_not_bounded_by_any_constant_multiple_of_object_count(self):
        """States the property's actual inequality and shows it fails: the
        brief's requirement is `work(x) <= K * n_objects(x)` for some
        constant K independent of x. Fit K generously from the SMALLEST
        measurement and show it is violated by more than three orders of
        magnitude at a modestly larger depth -- not a coincidence of a
        badly chosen K."""
        small = measure_via_canonical_bytes(8, canonical_bytes_fn=cn.canonical_bytes)
        large = measure_via_canonical_bytes(20, canonical_bytes_fn=cn.canonical_bytes)
        assert small.raised is None and large.raised is None
        k = small.bytes_emitted / small.n_distinct_objects
        predicted_if_bounded = k * large.n_distinct_objects
        assert large.bytes_emitted > 1000 * predicted_if_bounded, (
            f"K={k:.2f} bytes/object from depth 8 ({small.bytes_emitted} "
            f"bytes / {small.n_distinct_objects} objects) predicts "
            f"~{predicted_if_bounded:.0f} bytes at depth 20's "
            f"{large.n_distinct_objects} objects if work were truly "
            f"object-count-bounded; production actually emitted "
            f"{large.bytes_emitted} bytes -- a real object-count bound "
            "would make this assertion fail, which is the point")


# =====================================================================
# 3. THE ADMISSION WALK HANGS OUTRIGHT AT THE REVIEWER'S OWN DEPTH.
#    Run via the parent-supervised subprocess timeout (A4) so a genuine
#    non-return cannot hang this suite.
# =====================================================================

class TestAdmissionWalkHangsAtReviewerDepth:
    def test_depth_61_admission_walk_now_returns_a_bounded_refusal(self):
        """DELIBERATE LEDGER UPDATE (KALSHI-ARCHIVE-CORE-REMEDIATION-003
        defect B): "25 objects becomes 67 MB in 31 s, 61 objects never
        returns" was the reviewer's own finding, reproducing the SAME call
        path the review found: `SegmentWriter.submit()` -> `_admit()` ->
        `canonicalize_or_reason()` -> `non_canonical_reason`'s structural
        walk, which runs PRE-ACCEPTANCE, inside `_inflight`. Defect B added
        a shared, TOTAL work budget (`canonical.WorkBudget`, threaded
        through `segment._structural_reason` as well as `canonical._encode`)
        that is consumed once per node VISITED regardless of local
        depth/width legality -- at depth 61 the total node count vastly
        exceeds `CapabilityLimits.MAX_CANONICAL_WORK_UNITS` long before the
        walk would otherwise still be running, so this now returns quickly
        with a typed refusal instead of hanging.
        """
        script = """
from app.realtime import segment as sg
x = 0
for _ in range(61):
    x = [x, x]
r = sg.non_canonical_reason(x)
print("RESULT:" + repr(r))
"""
        v = run_with_parent_timeout(script, timeout_s=4.0, repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", (
            f"expected the admission walk at depth 61 to return quickly "
            f"with a bounded refusal now that defect B is fixed; got "
            f"{v.classification} instead (stdout={v.stdout!r} "
            f"stderr={v.stderr[-300:]!r})")
        assert "RESULT:None" not in v.stdout, (
            "depth 61 must be REFUSED (aggregate work over budget), not "
            f"silently admitted: {v.stdout!r}")
        assert "exceeded the total aggregate work bound" in v.stdout, v.stdout

    def test_a_moderate_depth_the_walk_can_still_finish_at_is_a_true_negative(self):
        """Calibration control: depth 15 must NOT time out, or the assertion
        above would be meaningless (the timeout mechanism itself would be
        the reason, not the aggregate-work gap)."""
        script = """
from app.realtime import segment as sg
x = 0
for _ in range(15):
    x = [x, x]
r = sg.non_canonical_reason(x)
print("RESULT:" + repr(r))
"""
        v = run_with_parent_timeout(script, timeout_s=10.0, repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", v
        assert "RESULT:None" in v.stdout, v.stdout  # admitted, not refused


# =====================================================================
# 4. DISCRIMINATION: the bad-by-construction encoder (production's class of
#    omission) is caught; the good, aggregate-bounded encoder is not.
# =====================================================================

class TestDiscriminationPlantedEncoders:
    def _work_bounded_by_object_count(self, encoder_cls, *, depth: int) -> bool:
        """Runs the property check against a planted encoder: does its
        visit count stay within a modest constant multiple of the
        structure's true object count, or does it explode?"""
        x = make_shared_dag(depth)
        n_obj = count_distinct_objects(x)
        enc = encoder_cls()
        try:
            enc.walk(x)
        except AggregateWorkExceeded:
            return True     # refused BEFORE exploding -- bounded by design
        # If it didn't refuse, its visit count must still be reasonably
        # close to the object count for the property to hold.
        return enc.visits <= 50 * n_obj

    def test_bad_encoder_without_an_aggregate_bound_violates_the_property(self):
        """The PLANTED BAD ENCODER: mirrors production's omission (depth +
        local width only). Must be caught -- i.e. the property check above
        must return False for it at a depth where the exponential blow-up
        has clearly overtaken any reasonable object-count multiple."""
        depth = 16                       # 2**16 - 1 = 65,535 visits, vs 17 objects
        x = make_shared_dag(depth)
        n_obj = count_distinct_objects(x)
        enc = BadEncoderNoAggregateBound()
        enc.walk(x)
        assert enc.visits > 1000 * n_obj, (
            f"expected the bad encoder's visit count ({enc.visits}) to "
            f"dwarf any reasonable multiple of the object count "
            f"({n_obj}) at depth {depth} -- if it doesn't, this "
            "planted control is not actually reproducing the omission "
            "class it claims to")
        assert not self._work_bounded_by_object_count(
            BadEncoderNoAggregateBound, depth=depth), (
            "the aggregate-work property check FAILED TO CATCH the "
            "planted bad encoder -- the harness would be worthless")

    def test_good_encoder_with_a_declared_aggregate_bound_satisfies_the_property(self):
        """The compliant control: refuses BEFORE the exponential blow-up
        materialises, because it counts TOTAL visits against a declared
        ceiling regardless of local depth/width legality. This is the
        shape production's `CapabilityLimits` would need to gain."""
        depth = 16
        assert self._work_bounded_by_object_count(
            GoodEncoderWithAggregateBound, depth=depth), (
            "the good, aggregate-bounded encoder should satisfy the "
            "property (either by staying within a modest visit multiple "
            "or by refusing outright) -- if this fails, the property "
            "statement itself, not production, needs re-examining")
        # And it must actually be the REFUSAL path that saves it here --
        # confirms this is a real bound, not an accidentally-small depth.
        x = make_shared_dag(depth)
        enc = GoodEncoderWithAggregateBound()
        with pytest.raises(AggregateWorkExceeded):
            enc.walk(x)
        assert enc.visits <= GoodEncoderWithAggregateBound.MAX_TOTAL_VISITS + 1

    def test_good_encoder_still_admits_ordinary_small_structures(self):
        """Negative control on the control: the aggregate bound must not
        itself be so tight that it refuses ordinary, non-adversarial
        input -- a bound is only meaningful if legitimate traffic still
        passes."""
        ordinary = {"a": [1, 2, 3], "b": {"c": "d"}}
        enc = GoodEncoderWithAggregateBound()
        # GoodEncoder's walk() only understands list/scalar for this proof;
        # exercise it on a plain nested list instead, which is enough to
        # show small legitimate structures are unaffected.
        enc.walk([1, [2, 3], [4, [5, 6]]])
        assert enc.visits < 20
