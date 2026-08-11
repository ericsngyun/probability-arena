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

    def test_end_to_end_through_a_real_writer_accepted_then_segment_destroyed(
            self, tmp_path):
        env = minimal_key_envelope_at_admission_ceiling()
        assert sg.non_canonical_reason(env) is None

        w, root = make_writer(tmp_path, "seg-work-unit-margin")
        # ACCEPTED -- submit() returns None, the exact contract a producer
        # relies on to know the event is durable.
        result = w.submit(env)
        assert result is None, (
            f"expected ACCEPTED (None); got {result!r} -- if admission now "
            "refuses this value, the ceiling-tuning in "
            "minimal_key_envelope_at_admission_ceiling needs re-deriving "
            "for the current CapabilityLimits")

        deadline_iterations = 0
        while w.accounting.written == 0 and w.accounting.failed_after_accept == 0:
            deadline_iterations += 1
            assert deadline_iterations < 2000, "writer thread never finished"
            import time
            time.sleep(0.005)

        assert w.accounting.failed_after_accept == 1, (
            f"expected the writer thread to have failed this record after "
            f"accepting it: {w.accounting.to_dict()}")
        assert w.accounting.written == 0

        # `close()` REFUSES to publish -- an accepted record that failed
        # after acceptance makes `clean()` false, and this is the ONLY
        # record in the segment: the entire segment is lost, not merely
        # this one record, because a segment cannot publish partially.
        with pytest.raises(sg.SegmentError, match="not written"):
            w.close()
        assert w.state is sg.SegmentState.INVALID


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

    def test_end_to_end_a_legally_admitted_metadata_value_kills_the_whole_segment(
            self, tmp_path):
        meta = nested_mapping(MAX_DEPTH)
        assert sg.non_canonical_reason(meta) is None

        w, root = make_writer(tmp_path, "seg-depth-mismatch",
                              subscription_metadata=meta)
        # The constructor's own admission gate accepted it -- exactly the
        # contract `SegmentWriter.__init__`'s docstring states ("A failure
        # now surfaces at construction, as a SegmentError, before any
        # record is ever accepted").
        assert w.subscription_metadata is not None

        # Ordinary records write and commit normally -- the defect is
        # specific to the metadata wrapping, not to the writer generally.
        for i in range(5):
            assert w.submit(fields(i)) is None
        deadline_iterations = 0
        while w.accounting.written < 5:
            deadline_iterations += 1
            assert deadline_iterations < 2000, "writer thread never finished"
            import time
            time.sleep(0.005)

        # THE VIOLATION: five genuinely durable, already-written,
        # already-verifiable records are destroyed at close because a
        # value admitted as legal at construction fails to encode one
        # level deeper than admission ever checked.
        #
        # NOTE ON THE EXACT FAILURE SHAPE: `build_manifest` itself
        # SUCCEEDS -- `subscription_metadata_digest` is computed as its own
        # ROOT-level digest (`digest_hex(subscription_metadata or {})`,
        # same depth as admission checked), so `_close_stages`'s narrower
        # `except CanonicalError` block around `build_manifest`
        # (documented as catching exactly this class, with the friendlier
        # "subscription_metadata could not be canonically digested at
        # close" message) never fires. The depth violation instead surfaces
        # one call later, inside `publish_manifest`'s
        # `canonical_bytes(manifest)` (the WHOLE manifest dict, with
        # `subscription_metadata` nested one level inside it) -- caught only
        # by the generic `except BaseException` around `publish_manifest`,
        # producing a less specific "manifest publication failed at stage
        # 'fsync'" message. Both are SegmentError; the segment is destroyed
        # either way -- this is a second, smaller finding (the diagnostic
        # message's own precondition is stale) riding along with the main
        # one.
        with pytest.raises(
                sg.SegmentError,
                match="manifest publication failed"):
            w.close()
        assert w.state is sg.SegmentState.INVALID
        # `verify_segment` on the untouched-on-disk evidence shows exactly
        # what was lost: real, chain-valid records with no manifest to
        # ever certify them, because close() never reached publication.
        verdict = sg.verify_segment(w.dir, environment=ENV, allow_open=True,
                                    root=root)
        assert verdict.records_read == 5, verdict


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
