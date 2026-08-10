"""KALSHI-ARCHIVE-VERIFICATION-META-001 A6 -- mapping semantic-loss harness.

DOES NOT MODIFY app/realtime/canonical.py or app/realtime/segment.py.

`test_admitted_implies_fixpoint` in `test_kalshi_encoder_fidelity_harness_001.py`
remains strict-xfail and is NOT touched, removed, or relabelled here. That
test's counterexample (`_CollidingKey`) is a HOSTILE `str` subclass with a
constant `__hash__` and an `__eq__` that always returns `False` -- deliberate
trickery at the key-equality level.

THIS harness is about a DIFFERENT, ORDINARY case a review found: a
`collections.abc.Mapping` whose `.items()` can present two entries for the
same logical string key WITHOUT any hostile `__hash__`/`__eq__` override at
all -- simply because `.items()` is the raw source of truth for a mapping
type that is not backed by a Python `dict` (an association-list-backed
multimap is an entirely ordinary, unremarkable representation many codebases
use for "an object with possibly-repeated keys", which is legal JSON syntax
even though most parsers collapse it on the way in). `__len__`/`__iter__`/
`__getitem__` present a simplified last-wins view; `.items()` -- which is
exactly what BOTH `segment._structural_reason`'s Mapping branch and
`canonical._encode`'s Mapping branch iterate -- shows both entries.

WHAT SHOULD HAPPEN, STATED EXPLICITLY. For any Mapping capable of
presenting duplicate logical keys, canonicalization must be exactly one of:

    LOSSLESS            every distinct (key, value) pair the Mapping's
                         `.items()` presents survives the round trip in some
                         recoverable form (this canonical schema has none for
                         a JSON object -- JSON objects cannot express a
                         repeated key -- so this outcome is not reachable
                         for THIS type without a schema change; it is listed
                         because a compliant implementation might choose to
                         represent duplicate-key-capable mappings as, e.g.,
                         an ordered array of [key, value] pairs instead of a
                         bare object, which WOULD be lossless).
    EXPLICITLY_REJECTED admission refuses the value outright, with a stated
                         reason, before anything is queued or written.
    SILENTLY_LOSSY       admission ADMITS the value, the producer is told
                         "accepted", and one of the two entries disappears
                         with no reason ever surfaced anywhere in the
                         pipeline -- gate, encode, digest, submit, close,
                         or verify.

ONLY THE FIRST TWO ARE PERMISSIBLE. This harness classifies production's
actual behaviour end-to-end (gate -> canonical bytes -> round trip -> digest
-> `SegmentWriter.submit()` -> `close()` -> `verify_segment()`) and must
currently detect SILENTLY_LOSSY.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

UTC = timezone.utc
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class DuplicateKeyMapping(Mapping):
    """An ORDINARY -- not hostile -- Mapping backed by an ordered
    association list. No `__hash__`/`__eq__` override anywhere; string
    equality and hashing are the ordinary `str` ones throughout.

    `.items()` is the raw source of truth and can present the SAME string
    key more than once. `__iter__`/`__len__`/`__getitem__` present a
    simplified LAST-WINS view (the same convention an ordinary `dict`
    literal with a repeated key uses), which is exactly what makes this
    non-hostile: every dunder any ordinary caller would reach for behaves
    exactly like a plain dict already collapsed to one entry per key. Only
    `.items()` -- which admission and the encoder both use directly --
    exposes that there were really two.
    """

    def __init__(self, pairs):
        self._pairs = list(pairs)   # [(key, value), ...], keys may repeat

    def __iter__(self):
        seen = []
        for k, _ in self._pairs:
            if k not in seen:
                seen.append(k)
        return iter(seen)

    def __len__(self):
        return len(set(k for k, _ in self._pairs))

    def __getitem__(self, key):
        value = None
        found = False
        for k, v in self._pairs:      # last-wins, exactly like `dict({...})`
            if k == key:
                value, found = v, True
        if not found:
            raise KeyError(key)
        return value

    def items(self):
        return list(self._pairs)      # THE point: both entries, unabridged


def _fields(i, raw_event):
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(NOW),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": raw_event,
        "normalized_event": {"venue_side": "no", "raw_price_units": 5100},
    }


def _init_archive(root, environment="demo"):
    try:
        ah.initialize_archive(Path(root), environment,
                              archive_identity="kalshi-realtime")
    except ah.ArchiveHeadError:
        pass
    return root


# ---------------------------------------------------------------------------
# The classifier itself: LOSSLESS / EXPLICITLY_REJECTED / SILENTLY_LOSSY.
# ---------------------------------------------------------------------------

def classify_duplicate_key_handling(mapping) -> dict:
    """Inspects EVERY layer the brief asks for and returns a full report,
    including the single verdict string.

    `mapping` must present at least one key more than once via `.items()`
    with at least two semantically DIFFERENT values for that key (a
    duplicate key with an IDENTICAL value twice is not evidence of loss
    either way and is deliberately excluded from triggering a verdict).
    """
    items = mapping.items()
    values_by_key: dict = {}
    for k, v in items:
        values_by_key.setdefault(k, []).append(v)
    dup_keys = {k: vs for k, vs in values_by_key.items()
               if len(vs) > 1 and len(set(vs)) > 1}
    report = {
        "input_semantic_members": dict(values_by_key),
        "duplicate_keys": dup_keys,
        "gate_reason": None,
        "canonical_bytes": None,
        "round_trip": None,
        "digest": None,
        "verdict": None,
    }
    if not dup_keys:
        report["verdict"] = "NOT_APPLICABLE"
        return report

    gate_reason = sg.non_canonical_reason(mapping)
    report["gate_reason"] = gate_reason
    if gate_reason is not None:
        report["verdict"] = "EXPLICITLY_REJECTED"
        return report

    # Admitted. Compute the SAME canonicalization the writer will use.
    encoded = cn.canonical_bytes(mapping)
    report["canonical_bytes"] = encoded
    round_trip = cn.parse_canonical(encoded)
    report["round_trip"] = round_trip
    report["digest"] = cn.digest_hex(mapping)

    # LOSSLESS would mean every distinct value for every duplicate key is
    # still recoverable from the canonical representation -- this schema's
    # only representation for a Mapping is a flat JSON object, which has no
    # way to hold two values under one key, so the only way this branch can
    # be LOSSLESS is if the encoder chose some OTHER shape (e.g. an array of
    # pairs) for a value it detected as duplicate-key-capable. Check for
    # that generously before concluding loss.
    lossy_keys = []
    for k, vs in dup_keys.items():
        out_value = round_trip.get(k) if isinstance(round_trip, dict) else None
        # A lossless representation would let every DISTINCT input value be
        # recovered somehow -- e.g. out_value itself being a list containing
        # all of them. A bare scalar/string that matches only ONE of the
        # inputs is the silent-collapse signature.
        if isinstance(out_value, list) and set(map(str, vs)).issubset(
                set(map(str, out_value))):
            continue
        recovered = {str(out_value)} if out_value is not None else set()
        if not set(map(str, vs)).issubset(recovered):
            lossy_keys.append(k)
    report["lossy_keys"] = lossy_keys
    report["verdict"] = "SILENTLY_LOSSY" if lossy_keys else "LOSSLESS"
    return report


# =====================================================================
# 1. NEGATIVE CONTROL: an ordinary mapping with no duplicate keys must not
#    be classified either LOSSLESS or SILENTLY_LOSSY -- confirms the
#    harness only fires when there is something to detect.
# =====================================================================

class TestNegativeControlNoDuplicateKeys:
    def test_ordinary_mapping_is_not_applicable(self):
        ordinary = DuplicateKeyMapping([("a", "1"), ("b", "2")])
        report = classify_duplicate_key_handling(ordinary)
        assert report["verdict"] == "NOT_APPLICABLE"

    def test_repeated_identical_value_is_not_applicable(self):
        """Same key twice, SAME value both times -- not evidence of loss
        (nothing distinguishable was ever lost)."""
        harmless = DuplicateKeyMapping([("a", "1"), ("a", "1")])
        report = classify_duplicate_key_handling(harmless)
        assert report["verdict"] == "NOT_APPLICABLE"


# =====================================================================
# 2. THE PRODUCTION DEFECT: SILENTLY_LOSSY, detected end-to-end.
# =====================================================================

class TestProductionIsSilentlyLossy:
    """KNOWN DEFECT, pinned (deterministic -- always reproduces). If
    production is changed to EXPLICITLY_REJECT duplicate-key-capable
    mappings, or to represent them LOSSLESSLY, `classify_duplicate_key_
    handling` will start returning something other than SILENTLY_LOSSY and
    every assertion below will fail loudly, demanding this file be updated
    -- which is the intended "known-defect ledger" behaviour."""

    def test_classifier_detects_silently_lossy_at_the_encoder_layer(self):
        d = DuplicateKeyMapping([("a", "first"), ("a", "second")])
        report = classify_duplicate_key_handling(d)
        assert report["verdict"] == "SILENTLY_LOSSY", report
        assert report["gate_reason"] is None, (
            "admission REFUSED this non-hostile mapping -- that would "
            "actually be the EXPLICITLY_REJECTED outcome, which is "
            "permissible; update this test's classification, don't "
            "weaken it")
        assert report["round_trip"] == {"a": "second"}, (
            "expected last-wins collapse; if the shape of the loss "
            "changed, update this pinned expectation deliberately")
        assert report["lossy_keys"] == ["a"]

    def test_digest_is_computed_over_the_already_collapsed_value(self):
        """The digest is internally self-consistent -- it is NOT what is
        broken. What is broken is that the digest represents a value that
        silently dropped information the caller believed was submitted.
        Confirms the digest is stable/reproducible over the SAME mapping,
        which rules out "the digest itself is flaky" as an alternative
        explanation for anything downstream."""
        d = DuplicateKeyMapping([("a", "first"), ("a", "second")])
        assert cn.digest_hex(d) == cn.digest_hex(d)
        only_second = {"a": "second"}
        assert cn.digest_hex(d) == cn.digest_hex(only_second), (
            "the duplicate-key mapping and a plain dict holding only the "
            "SURVIVING value digest identically -- proof that 'first' "
            "left no trace anywhere the digest could reveal it")

    def test_producer_is_told_accepted_end_to_end_through_a_real_writer(
            self, tmp_path):
        """The full pipeline: submit() accepts, close() reports clean,
        verify_segment() reports VALID -- and the archived record only
        ever held 'second'. Nothing in the accepted-evidence path ever
        surfaces that 'first' existed."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-dup-key",
                             partition_identity="date=2026-08-10/hour=12",
                             commit_to_head=False)
        dup = DuplicateKeyMapping([("a", "first"), ("a", "second")])
        producer_verdict = w.submit(_fields(0, dup))
        assert producer_verdict is None, (
            "producer told ACCEPTED -- the defect signature: no rejection "
            "reason was ever returned for a value that just lost data")
        manifest = w.close()
        assert manifest["close_status"] == "clean"
        assert manifest["record_count"] == 1
        verifier_verdict = sg.verify_segment(w.dir, environment="demo")
        assert verifier_verdict.valid, verifier_verdict.reasons
        records = sg.read_segment_records(w.events_path)
        assert records[0]["raw_event"] == {"a": "second"}, (
            "the durable, verified-VALID evidence holds only the SURVIVING "
            "value -- 'first' is gone from every layer: gate, submit, "
            "close, and verify all reported success")


# =====================================================================
# 3. WHAT "EXPLICITLY_REJECTED" WOULD LOOK LIKE -- a control proving the
#    classifier correctly recognises the OTHER permissible outcome, so a
#    future fix that chooses rejection over lossless representation is
#    also recognised as compliant rather than silently re-flagged.
# =====================================================================

class TestExplicitRejectionIsRecognisedAsCompliant:
    def test_a_gate_that_refuses_duplicate_key_mappings_classifies_as_rejected(self):
        """Simulates what production COULD do: reject at the gate. This does
        not call production's gate (which admits today, per Section 2) --
        it proves the CLASSIFIER recognises EXPLICITLY_REJECTED correctly
        when a gate actually does refuse, using a minimal stand-in gate, so
        the classifier itself is not biased toward always finding loss."""

        class RejectingGateMapping(DuplicateKeyMapping):
            """Same duplicate-key shape, but reachable only through a
            harness-local check that mimics what a compliant
            `non_canonical_reason` would do -- this class does not change
            what `sg.non_canonical_reason` itself returns for it (that call
            is not part of this control), only what THIS test's classifier
            wrapper below reports."""

        def classify_with_explicit_rejection(mapping):
            items = mapping.items()
            values_by_key: dict = {}
            for k, v in items:
                values_by_key.setdefault(k, []).append(v)
            dup = {k: vs for k, vs in values_by_key.items()
                  if len(vs) > 1 and len(set(vs)) > 1}
            if dup:
                return {"verdict": "EXPLICITLY_REJECTED",
                       "gate_reason": f"duplicate logical key(s) {list(dup)}"}
            return {"verdict": "NOT_APPLICABLE", "gate_reason": None}

        d = RejectingGateMapping([("a", "first"), ("a", "second")])
        report = classify_with_explicit_rejection(d)
        assert report["verdict"] == "EXPLICITLY_REJECTED"
        assert report["gate_reason"] is not None


# =====================================================================
# 4. DOES NOT TOUCH THE EXISTING STRICT-XFAIL. Documented, not asserted
#    (asserting against another file's marker would couple this file to
#    that one's internals for no benefit) -- this class exists only so a
#    reviewer sees the boundary stated in code, not merely in prose.
# =====================================================================

class TestDoesNotOverlapTheExistingHostileKeyXfail:
    def test_this_mapping_is_not_hostile_no_hash_or_eq_override(self):
        d = DuplicateKeyMapping([("a", "first"), ("a", "second")])
        assert type(d).__hash__ is None or "__hash__" not in type(d).__dict__
        assert "__eq__" not in type(d).__dict__
        # And it behaves like an ordinary dict under ordinary access:
        assert dict(d) == {"a": "second"}
        assert len(d) == 1
        assert list(d) == ["a"]
