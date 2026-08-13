"""KALSHI-ARCHIVE-VERIFICATION-META-001 A5 -- the AGGREGATE-WORK property.

WHY THIS EXISTS. `canonical.CapabilityLimits` bounds nesting DEPTH
(`MAX_DEPTH`) and PER-CONTAINER width (`MAX_SEQUENCE_ELEMENTS`,
`MAX_MAPPING_ELEMENTS`) -- both checked LOCALLY, one container at a time, by
`segment._structural_reason` (admission) and `canonical._encode` (the
encoder). Neither bounds the PRODUCT of those numbers across the whole
structure: the TOTAL number of scalar/container visits the walk performs.

A reviewer reproduced this exactly:

    x = 0
    for _ in range(60):
        x = [x, x]

Twenty-five Python objects (`id(x)` is reused at every level -- `x` at depth
d is referenced from BOTH slots of the list at depth d+1, so the number of
DISTINCT objects reachable is `depth + 1`, not `2**depth`) sit at depth 60,
well inside `MAX_DEPTH = 256`; every container has exactly 2 elements, well
inside `MAX_SEQUENCE_ELEMENTS = 1_000_000`. Every LOCAL check `segment.py`
and `canonical.py` perform passes. But neither `_structural_reason` nor
`_encode` (nor `json.dumps`, which has no notion of object identity) ever
memoises a repeated `id()` -- JSON itself has no way to express "this
subtree is the same object as that one" -- so the walk necessarily visits
every one of the `2**depth - 1` LEAF PATHS through the tree once each. At
depth 25 that materialised as 67 MB in 31 seconds; at depth 61 ("61 objects
never returns") the walk simply never completes, and it hangs the
PRE-ACCEPTANCE admission walk running under `SegmentWriter._admission` /
`_inflight` -- meaning it can also produce the A8 `_inflight`-stuck class
described in that harness, not merely a slow write.

THE PROPERTY, STATED EXPLICITLY (not fitted to what happens to pass):

    AGGREGATE-WORK BOUND. For any value `x` legally admitted under
    `CapabilityLimits` (local depth <= MAX_DEPTH, local width <=
    MAX_SEQUENCE_ELEMENTS/MAX_MAPPING_ELEMENTS at every container), the TOTAL
    work performed by canonicalizing `x` -- measured as either (a) the
    number of scalar/container visits the walk performs, or (b) the number
    of bytes `canonical_bytes(x)` emits -- must be bounded by a function of
    the number of DISTINCT PYTHON OBJECTS reachable in `x` (`work(x) <= K *
    n_objects(x)` for some constant K independent of x's sharing
    structure), NOT by a function that can be exponential in depth for a
    structure with a small, fixed object count.

    Equivalently: a capability contract that declares "depth <= 256" and
    "width <= 1,000,000 per container" but enforces no separate AGGREGATE
    bound on total visits/bytes has not actually bounded canonicalization's
    resource usage at all -- the worst case admissible under those two
    numbers ALONE is `1_000_000 ** 256`, and even a tiny, deliberately
    constructed corner of that space (width 2, depth 61) already never
    returns. "Every local check passed" is not "the operation is bounded".

THIS PROPERTY IS NOT SATISFIED BY PRODUCTION TODAY. `CapabilityLimits`
declares no `MAX_TOTAL_NODES` (or equivalent) and neither `_structural_reason`
nor `_encode` counts a running total across the whole call -- only per
container. This module states the property and measures against it; it does
not pick a bound after the fact to make a test pass.

MEASUREMENT, NOT MODIFICATION: nothing here monkeypatches, wraps, or edits
`app.realtime.canonical`/`app.realtime.segment`. Work is measured
EXTERNALLY: bytes emitted (`len(canonical_bytes(x))`), wall time, and (for
the standalone reference walkers below, built for the discrimination proof
only) an explicit visit counter.
"""

from __future__ import annotations

from dataclasses import dataclass


def make_shared_dag(depth: int, leaf=0):
    """`x = leaf; for _ in range(depth): x = [x, x]`.

    Exactly `depth + 1` distinct Python objects are reachable (verify with
    `count_distinct_objects`) -- SHARING, not duplication: the SAME list
    object sits in both slots of its parent. Every container has width 2
    (legal under any real `MAX_SEQUENCE_ELEMENTS`) and the whole structure
    sits at depth `depth` (legal under `MAX_DEPTH` for any `depth <= 256`).
    A walk with no memoisation of `id()` -- which is what both
    `segment._structural_reason` and `canonical._encode` are -- visits
    `2**depth - 1` list nodes.
    """
    x = leaf
    for _ in range(depth):
        x = [x, x]
    return x


def count_distinct_objects(x, *, _seen: set | None = None) -> int:
    """The Python object count `make_shared_dag` claims: reachable objects
    counted by `id()`, so sharing collapses to one regardless of how many
    paths reach it. This is what a MEMOISING walk (one that actually bounds
    total work by object count, not by path count) would visit exactly
    once each.
    """
    if _seen is None:
        _seen = set()
    oid = id(x)
    if oid in _seen:
        return 0
    _seen.add(oid)
    total = 1
    if isinstance(x, list):
        for item in x:
            total += count_distinct_objects(item, _seen=_seen)
    return total


@dataclass
class WorkMeasurement:
    depth: int
    n_distinct_objects: int
    bytes_emitted: int | None
    duration_s: float
    raised: str | None = None       # exception type name, if the walk refused


def measure_via_canonical_bytes(depth: int, *, canonical_bytes_fn) -> WorkMeasurement:
    """Measure PRODUCTION's actual `canonical_bytes` against the generator.
    Bytes emitted is an external, non-invasive proxy for total encoder work:
    JSON cannot express sharing, so every one of the `2**depth` leaf paths
    literally has to appear as text in the output when no aggregate bound
    refuses the value first.
    """
    import time as _time

    x = make_shared_dag(depth)
    n_obj = count_distinct_objects(x)
    start = _time.monotonic()
    try:
        b = canonical_bytes_fn(x)
        return WorkMeasurement(depth, n_obj, len(b), _time.monotonic() - start)
    except Exception as exc:              # noqa: BLE001 - refusal, not a crash
        return WorkMeasurement(depth, n_obj, None, _time.monotonic() - start,
                               raised=type(exc).__name__)


# ---------------------------------------------------------------------------
# Standalone reference walkers, used ONLY to prove the methodology itself
# discriminates a compliant design (one that declares and enforces an
# aggregate bound) from a non-compliant one (one that, like production
# today, bounds only depth and local width) -- the same role
# `harness_encoder_fidelity/reference_and_broken.py` and
# `harness_async_accounting/reference_shim.py` play for their milestones.
# Neither of these is imported by, patches, or otherwise touches
# `app.realtime.*`.
# ---------------------------------------------------------------------------

class AggregateWorkExceeded(RuntimeError):
    """Raised by `GoodEncoder` when the DECLARED total-work budget -- not
    merely a per-container width -- would be exceeded."""


class BadEncoderNoAggregateBound:
    """Mirrors the OMISSION that makes production non-compliant: depth and
    per-container width are checked, nothing bounds the total. A standalone
    reproduction of the SAME CLASS of gap (not a copy of production's
    source), so the property test demonstrates it catches the class, not
    one specific implementation's accident.
    """

    MAX_DEPTH = 256
    MAX_WIDTH = 1_000_000

    def __init__(self):
        self.visits = 0

    def walk(self, value, _depth: int = 0):
        self.visits += 1
        if _depth > self.MAX_DEPTH:
            raise ValueError(f"depth exceeds {self.MAX_DEPTH}")
        if isinstance(value, list):
            if len(value) > self.MAX_WIDTH:
                raise ValueError(f"width exceeds {self.MAX_WIDTH}")
            for item in value:
                self.walk(item, _depth + 1)      # NO memoisation of id(item)


class GoodEncoderWithAggregateBound:
    """The compliant shape: a SEPARATE, DECLARED ceiling on the TOTAL number
    of visits across the whole call, checked on every single visit
    regardless of depth or local width -- so `work(x)` is bounded by a
    CONSTANT (`MAX_TOTAL_VISITS`) independent of how deep or how shared `x`
    is. This is what `CapabilityLimits` would need to add (e.g.
    `MAX_TOTAL_NODES`) for production to satisfy the property this module
    states; it does not have to be *this specific* strategy (a memoising
    walk that counts each distinct `id()` once would also satisfy the
    property, at the cost of changing what canonical bytes a shared
    structure produces -- a much larger design change out of this
    milestone's scope to propose).
    """

    MAX_DEPTH = 256
    MAX_WIDTH = 1_000_000
    MAX_TOTAL_VISITS = 5_000

    def __init__(self):
        self.visits = 0

    def walk(self, value, _depth: int = 0):
        self.visits += 1
        if self.visits > self.MAX_TOTAL_VISITS:
            raise AggregateWorkExceeded(
                f"canonicalization visited more than {self.MAX_TOTAL_VISITS} "
                "nodes in this call -- refusing regardless of local "
                "depth/width legality")
        if _depth > self.MAX_DEPTH:
            raise ValueError(f"depth exceeds {self.MAX_DEPTH}")
        if isinstance(value, list):
            if len(value) > self.MAX_WIDTH:
                raise ValueError(f"width exceeds {self.MAX_WIDTH}")
            for item in value:
                self.walk(item, _depth + 1)
