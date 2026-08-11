"""KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4 -- budget consistency between
admission and commit.

Two real, end-to-end reproductions through an unmodified `SegmentWriter`
(see `tests/meta_runtime/budget_consistency.py` for the full mechanism):

  1. a value admitted EXACTLY at the aggregate canonical work-unit ceiling
     is ACCEPTED by `submit()`, then destroys the whole segment at close
     because `build_record`'s wrapping adds enough extra top-level fields
     to push a FRESH commit-time budget over the same limit;
  2. `subscription_metadata` admitted at exactly the legal nesting depth
     ceiling is accepted at construction, then destroys the whole segment
     at close because `build_manifest`/`publish_manifest` nest it one
     level deeper than admission ever checked.

DOES NOT MODIFY `app/realtime/segment.py` or `app/realtime/canonical.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.meta_runtime.budget_consistency import (
    encode_units,
    minimal_key_envelope_at_admission_ceiling,
    nested_lists,
    nested_mapping,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
ENV = "demo"
LIMIT = cn.CapabilityLimits.MAX_CANONICAL_WORK_UNITS
MAX_DEPTH = cn.CapabilityLimits.MAX_DEPTH


def fields(i, **extra):
    base = {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW + timedelta(microseconds=i)),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": {"price_dollars": "0.5100", "side": "no"},
        "normalized_event": {"raw_price_units": 5100},
    }
    base.update(extra)
    return base


def make_writer(tmp_path, seg_id, **kw):
    root = tmp_path / seg_id
    root.mkdir()
    ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
    kw.setdefault("commit_to_head", False)
    return sg.SegmentWriter(root, environment=ENV, segment_id=seg_id,
                            partition_identity="p", **kw), root


# =====================================================================
# 1. WORK-UNIT MARGIN: minimal-mapping-key construction flips the
#    accidental "-5 unit" safety margin the production 10-key envelope
#    happens to enjoy.
# =====================================================================

class TestAsymmetryDemonstration:
    def test_structural_walk_charges_more_per_mapping_key_than_encode(self):
        """The exact mechanism, isolated and measured directly against the
        real functions (not asserted from the docstring's arithmetic)."""
        value = {"a": 1, "b": 2, "c": 3}       # 3 top-level mapping keys
        structural_budget = cn.WorkBudget(limit=10**9)
        assert sg._structural_reason(value, "", 0, structural_budget) is None
        encode_only = encode_units(value)
        assert structural_budget.units > encode_only, (
            f"expected the structural walk ({structural_budget.units} "
            f"units) to charge MORE than the encoder ({encode_only} units) "
            "for the same 3-key flat mapping -- if this is no longer true, "
            "the asymmetry this section documents has changed and every "
            "assertion below needs re-deriving")
        # Exactly 2 extra units per key: one for the key's own recursive
        # structural check, which `_encode`'s `_check_str(k, ...)` performs
        # WITHOUT touching the budget.
        assert structural_budget.units - encode_only == len(value)

    def test_minimal_key_envelope_admitted_at_ceiling_destroys_the_record_at_commit(
            self, tmp_path):
        env = minimal_key_envelope_at_admission_ceiling()
        # Admitted -- both halves of admission (structural walk AND the
        # encode `canonicalize_or_reason` also performs) agree it is legal.
        assert sg.non_canonical_reason(env) is None
        payload, bad = sg.canonicalize_or_reason(env)
        assert bad is None and payload is not None

        # `build_record` wraps it into the real 17-field record shape --
        # and computes `record_digest` via `digest_hex(...)` INTERNALLY, as
        # its very last step, so the failure below happens INSIDE
        # `build_record` itself, not in some later, separate encode call.
        # THE VIOLATION: a value admission certified as legal, wrapped
        # exactly the way the real writer wraps it, no longer encodes
        # within the SAME capability contract's own limit.
        with pytest.raises(cn.CanonicalError, match="aggregate work bound"):
            sg.build_record(
                envelope_fields=env, segment_id="seg", environment=ENV,
                previous_record_digest=sg.genesis_digest(
                    segment_id="seg", environment=ENV),
                receive_ordinal=0)

    def test_end_to_end_through_a_real_writer_refuses_cleanly_at_admission(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4 REPAIRED: `env` sits at
        the RAW (unreserved) admission ceiling -- `sg.non_canonical_reason`
        (called with no reservation, exactly as `minimal_key_envelope_at_
        admission_ceiling` tuned it) still says it is legal on its own. The
        REAL writer path (`w.submit` -> `_admit` -> `canonicalize_or_reason`
        with `_work_reserve=_RECORD_ENVELOPE_OVERHEAD_UNITS`) now reserves
        exactly the headroom `build_record`'s wrapper will need, so THIS
        value -- which used to be silently accepted and then destroy the
        segment at commit -- is refused cleanly at admission instead. No
        segment is ever touched; the producer is told NOT_CANONICAL and can
        act on it immediately, instead of being told ACCEPTED for a record
        that a background thread would destroy seconds later."""
        env = minimal_key_envelope_at_admission_ceiling()
        assert sg.non_canonical_reason(env) is None, (
            "the raw (unreserved) ceiling construction should still be "
            "legal on its own -- if this is no longer true, the "
            "ceiling-tuning needs re-deriving for the current "
            "CapabilityLimits")

        w, root = make_writer(tmp_path, "seg-work-unit-margin")
        result = w.submit(env)
        assert result == sg.RejectReason.NOT_CANONICAL, (
            f"expected the real writer's reserved admission budget to "
            f"refuse a value that only fits the wrapped record's budget by "
            f"accident -- got {result!r}. If admission now accepts this "
            "value, the ceiling-tuning in "
            "minimal_key_envelope_at_admission_ceiling needs re-deriving "
            "for the current CapabilityLimits/overhead reservation")
        assert w.last_rejection_detail is not None
        assert "aggregate work bound" in w.last_rejection_detail

        # No record was ever accepted -- close() publishes a genuinely
        # empty, clean segment rather than destroying one over a wrapper
        # overhead nobody outside `build_record` used to account for.
        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 0
        assert w.state is sg.SegmentState.CLOSED


# =====================================================================
# 2. DEPTH MISMATCH: subscription_metadata admitted at the legal depth
#    ceiling, destroyed one level deeper at close.
# =====================================================================

class TestSubscriptionMetadataDepthMismatch:
    def test_metadata_at_max_depth_is_admitted_directly(self):
        meta = nested_mapping(MAX_DEPTH)
        assert sg.non_canonical_reason(meta) is None
        assert cn.canonical_bytes(meta)   # encodes fine at its own root depth

    def test_wrapping_one_level_deeper_exceeds_the_same_bound(self):
        meta = nested_mapping(MAX_DEPTH)
        body = {"a": 1, "subscription_metadata": meta}
        with pytest.raises(cn.CanonicalError, match="depth bound"):
            cn.canonical_bytes(body)

    def test_end_to_end_metadata_at_the_raw_ceiling_is_now_refused_at_construction(
            self, tmp_path):
        """KALSHI-ARCHIVE-CORE-REMEDIATION-003B A4 REPAIRED: `meta` sits at
        the RAW (unreserved) depth ceiling -- `sg.non_canonical_reason`
        (called with no reservation) still says it is legal on its own. The
        constructor now validates `subscription_metadata` with
        `_depth_reserve=_MANIFEST_METADATA_DEPTH_RESERVE`, reserving the
        one level `build_manifest` will always add -- so THIS value, which
        used to be silently admitted and then destroy five already-durable
        records at close, is refused cleanly at construction instead, before
        the segment ever accepts a single record."""
        meta = nested_mapping(MAX_DEPTH)
        assert sg.non_canonical_reason(meta) is None, (
            "the raw (unreserved) ceiling construction should still be "
            "legal on its own -- if this is no longer true, MAX_DEPTH "
            "handling changed and this needs re-deriving")

        root = tmp_path / "seg-depth-mismatch"
        root.mkdir()
        ah.initialize_archive(root, ENV, archive_identity="kalshi-realtime")
        with pytest.raises(sg.SegmentError, match="depth bound"):
            sg.SegmentWriter(root, environment=ENV,
                             segment_id="seg-depth-mismatch",
                             partition_identity="p",
                             subscription_metadata=meta, commit_to_head=False)
        # Nothing was ever opened for this segment id -- construction failed
        # before any record could be accepted, let alone destroyed.
        assert not (root / f"env={ENV}" / "segment=seg-depth-mismatch"
                    / sg.MANIFEST_FILENAME).exists()

    def test_end_to_end_metadata_one_level_shallower_survives_the_real_wrap(
            self, tmp_path):
        """The other half of the property: a value that respects the
        reserved headroom is admitted AND survives being wrapped one level
        deeper by `build_manifest`/`publish_manifest` -- the reservation is
        exactly the overhead commit adds, not an over-broad refusal of
        everything near the ceiling."""
        meta = nested_mapping(MAX_DEPTH - 1)
        assert sg.non_canonical_reason(meta) is None

        w, root = make_writer(tmp_path, "seg-depth-ok",
                              subscription_metadata=meta)
        assert w.subscription_metadata is not None

        for i in range(5):
            assert w.submit(fields(i)) is None
        deadline_iterations = 0
        while w.accounting.written < 5:
            deadline_iterations += 1
            assert deadline_iterations < 2000, "writer thread never finished"
            import time
            time.sleep(0.005)

        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 5
        assert w.state is sg.SegmentState.CLOSED
        verdict = sg.verify_segment(w.dir, environment=ENV, root=root)
        assert verdict.valid, verdict.reasons
        assert verdict.records_read == 5


# =====================================================================
# 3. THE STATED PROPERTY, restated directly (not xfail: both reproductions
#    above already demonstrate the violation with a clear failure signature;
#    this section is the single, general assertion the milestone brief asks
#    for, generalised across every bound in CapabilityLimits it is
#    practical to construct a ceiling value for without an excessive
#    (>>1min) single-test runtime).
# =====================================================================

class TestGeneralPropertyAcrossCapabilityLimitsBounds:
    """For any value at the admission ceiling of a bound in
    `CapabilityLimits`, the committed record must encode within its OWN
    commit-time budget too. Every bound checked here is EXPECTED (not
    hoped) to hold, because the two failing bounds (aggregate work units,
    nesting depth) are already isolated and reproduced above -- this
    section is the NEGATIVE space, proving the property is not violated
    for bounds this milestone did NOT name as broken, so the fix (when it
    lands) has a green baseline to protect."""

    def test_max_int_bits_ceiling_round_trips_through_build_record(self, tmp_path):
        big = (1 << (cn.CapabilityLimits.MAX_INT_BITS - 1))
        env = fields(0, raw_event={"n": big})
        assert sg.non_canonical_reason(env) is None
        record = sg.build_record(
            envelope_fields=env, segment_id="seg", environment=ENV,
            previous_record_digest=sg.genesis_digest(
                segment_id="seg", environment=ENV),
            receive_ordinal=0)
        assert cn.canonical_bytes(record)

    def test_max_decimal_digits_ceiling_round_trips_through_build_record(
            self, tmp_path):
        from decimal import Decimal
        text = "1." + ("9" * (cn.CapabilityLimits.MAX_DECIMAL_DIGITS - 1))
        env = fields(0, raw_event={"n": Decimal(text)})
        assert sg.non_canonical_reason(env) is None
        record = sg.build_record(
            envelope_fields=env, segment_id="seg", environment=ENV,
            previous_record_digest=sg.genesis_digest(
                segment_id="seg", environment=ENV),
            receive_ordinal=0)
        assert cn.canonical_bytes(record)

    def test_max_string_length_ceiling_round_trips_through_build_record(
            self, tmp_path):
        s = "x" * cn.CapabilityLimits.MAX_STRING_LENGTH
        env = fields(0, raw_event={"s": s})
        assert sg.non_canonical_reason(env) is None
        record = sg.build_record(
            envelope_fields=env, segment_id="seg", environment=ENV,
            previous_record_digest=sg.genesis_digest(
                segment_id="seg", environment=ENV),
            receive_ordinal=0)
        assert cn.canonical_bytes(record)
