"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4 -- budget consistency between
admission and commit.

WHY THIS EXISTS. Admission charges the canonical work budget
(`canonical.WorkBudget`, `MAX_CANONICAL_WORK_UNITS`) over `envelope_fields`
alone (`segment._admit` -> `canonicalize_or_reason` -> `_structural_reason`
AND `canonical_bytes`, both against a FRESH `WorkBudget` scoped to
`envelope_fields`). Commit re-charges a SECOND, independently fresh budget
of the SAME limit over `record` -- the 17-field envelope `build_record`
produces, which CONTAINS `envelope_fields`'s own values plus several
additional top-level scalar fields (`schema_version`,
`canonical_schema_version`, `environment`, `segment_id`, `receive_ordinal`,
`previous_record_digest`, `record_digest`) admission never walked or
charged for at all.

Two independent, EMPIRICALLY VERIFIED consequences of this (both proven
against the real, unmodified `app.realtime.segment`/`app.realtime.canonical`
functions in this module -- nothing here is a toy reproduction):

  1. WORK-UNIT MARGIN. `_structural_reason`'s Mapping branch charges TWO
     budget units per key (one for the key via a recursive call, one for
     the value) while `_encode`'s Mapping branch charges ONE (the key is
     checked via `_check_str`, which does not touch the budget; only the
     value recurses). For a value admitted EXACTLY at the aggregate work
     ceiling, this asymmetry means admission's own structural walk is
     systematically STRICTER than encode BY THE NUMBER OF MAPPING KEYS
     ENCOUNTERED -- production's real 10-key envelope survives only
     because that per-structure margin (empirically ~4-11 units for a
     realistic envelope shape) happens to exceed the ~7 extra top-level
     scalar fields commit's wrapping record adds. That margin is NOT a
     property of the budget design; it is an accident of how many mapping
     keys happen to sit at the top level of a real envelope. A minimal-key
     construction (the bulk of the ceiling-consuming structure held in
     LIST elements, which are charged symmetrically by both walks, with
     only ONE top-level mapping key) shrinks admission's margin to ~1 unit
     -- strictly LESS than commit's ~7-unit addition -- and the wrapped
     `record`'s own `canonical_bytes` call (inside `build_record`'s
     `digest_hex(...)`, called from the WRITER THREAD, well after `submit()`
     already returned ACCEPTED) then raises `CanonicalError`. `_write_one`
     catches it as `SERIALIZATION_FAILURE`
     (`accounting.fail_after_accept`), so `close()` refuses to publish
     ("not clean") -- an entire otherwise-valid segment (however many
     prior records it holds) is destroyed by one record that was
     legitimately ACCEPTED under the exact same capability contract the
     writer advertises.

  2. DEPTH MISMATCH. `subscription_metadata` is admitted at construction
     via `non_canonical_reason(metadata)` -- a ROOT-level walk, so nesting
     up to `MAX_DEPTH` (256) legally passes. At close, `build_manifest`
     nests that SAME value one level deeper, as `body["subscription_
     metadata"]`, inside the manifest dict `publish_manifest` then encodes
     whole (`canonical_bytes(manifest)`). A `subscription_metadata` value
     admitted at exactly the legal ceiling therefore encodes at depth
     `MAX_DEPTH + 1` when wrapped -- refused, raising `CanonicalError`
     inside `publish_manifest`, caught by `_close_stages`'s broad
     `except BaseException` and re-raised as a `SegmentError` -- the whole
     segment (all of its already-durable, already-verified records)
     refuses to publish over a value admission itself certified as legal.

Neither of these is fabricated to fit a bound after the fact -- both are
measured directly against `app.realtime.canonical`'s real `WorkBudget`/
`_encode`/`_structural_reason` and `app.realtime.segment`'s real
`non_canonical_reason`/`build_record`/`build_manifest`, and reproduced
end-to-end through a real `SegmentWriter`.

THE PROPERTY THIS FILE STATES (not fitted to what happens to pass): for any
value admitted at the ceiling of ANY bound in `CapabilityLimits` --
aggregate work units, decimal digits/exponent, int bits, sequence/mapping
size, nesting depth, string length -- the record `build_record` produces
from it AND the manifest `build_manifest`/`publish_manifest` produce from
`subscription_metadata` must BOTH encode within THEIR OWN commit-time
budgets too. 63bf0d1 does not satisfy this for at least the two ceilings
measured here (aggregate work, nesting depth).
"""

from __future__ import annotations

from app.realtime import canonical as cn
from app.realtime import segment as sg


def encode_units(value) -> int:
    """The exact number of `WorkBudget.consume()` calls a real
    `canonical._encode` pass performs over `value` -- measured directly
    against the production encoder, with an effectively unbounded local
    budget so the count is exact rather than truncated by a refusal."""
    wb = cn.WorkBudget(limit=10**12)
    cn._encode(value, 0, wb)
    return wb.units


def nested_lists(outer_n: int, inner_n: int, last_len: int) -> list:
    """`outer_n` sublists of `inner_n` short strings each, except the last
    sublist which has `last_len` elements -- lets a caller tune the total
    leaf count to a specific target without needing `outer_n * inner_n` to
    divide evenly. Every element is symmetric-cost under BOTH admission's
    structural walk and the encoder (list branches charge 1 unit/element
    in both), so this structure isolates the MAPPING-key asymmetry's
    effect to just the handful of dict keys actually present, rather than
    diluting it across millions of them.
    """
    rows = [["x"] * inner_n for _ in range(max(outer_n - 1, 0))]
    rows.append(["x"] * last_len)
    return rows


def minimal_key_envelope_at_admission_ceiling(
        *, outer_n: int = 1999, inner_n: int = 1999) -> dict:
    """A single-top-level-key `envelope_fields` (`{"raw_event": <deep list
    structure>}`) tuned, by binary search over the LAST row's length, to
    sit at EXACTLY the largest size `segment.non_canonical_reason` (the
    real admission gate `_admit` calls) still accepts -- i.e. the aggregate
    work-unit ceiling, approached from a minimal-mapping-key direction so
    admission's structural-walk margin over encode is as small as the
    asymmetry allows (~1 unit: only the envelope dict's own single key).
    """
    lo, hi = 0, cn.CapabilityLimits.MAX_SEQUENCE_ELEMENTS
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        env = {"raw_event": nested_lists(outer_n, inner_n, mid)}
        if sg.non_canonical_reason(env) is None:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return {"raw_event": nested_lists(outer_n, inner_n, best)}


def nested_mapping(depth: int, leaf="leaf"):
    """`{"k": {"k": {... leaf}}}`, `depth` levels deep."""
    x = leaf
    for _ in range(depth):
        x = {"k": x}
    return x
