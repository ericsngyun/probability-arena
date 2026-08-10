"""KALSHI-ARCHIVE-VERIFICATION-HARNESSES-001, harness A2 -- encoder FIDELITY
and TERMINATION.

DOES NOT MODIFY app/realtime/canonical.py, app/realtime/segment.py,
app/realtime/archive.py or app/realtime/archive_head.py. Every test here
exercises those modules purely from the outside. Where this harness
documents a defect it deliberately does NOT fix, that is stated at the top of
the relevant class.

=====================================================================
THE FIDELITY CONTRACT (read this before reading the tests)
=====================================================================
`app/realtime/canonical.py`'s docstring states the design intent: type
fidelity is deliberately NOT preserved across a round trip; only the
FIXPOINT property is guaranteed --

    canonical_bytes(x) == canonical_bytes(parse_canonical(canonical_bytes(x)))

This harness tests something stronger and orthogonal to that: not "does the
byte-form stabilise" but "does the byte-form still MEAN the same thing as the
Python value that was admitted". A digest that stabilises over a corrupted
meaning is not evidence -- it is a fixpoint over noise. So this harness
states, explicitly, per type, what the one legitimate transformation is:

  int        -> JSON integer. LEGITIMATE: none (int is already exact in
               JSON). CORRUPTION: any value change.
  bool       -> JSON true/false, kept distinct from 0/1. LEGITIMATE: none.
               CORRUPTION: collapsing onto int.
  None       -> JSON null. LEGITIMATE: none. CORRUPTION: any change.
  str        -> JSON string, UTF-8. LEGITIMATE: none -- a string that is not
               strictly UTF-8 encodable (e.g. a lone surrogate) must be
               REFUSED at admission, never silently re-encoded to something
               else. CORRUPTION: any character substitution, escaping change
               that alters the decoded value, or truncation.
  Decimal    -> canonical decimal TEXT (str). LEGITIMATE: exponent notation
               collapsed to plain notation; trailing fractional zeros
               dropped; "-0" normalised to "0". These change the TEXT but
               not the NUMERIC VALUE -- Decimal(text) after these
               transformations must equal the original Decimal exactly,
               digit for digit. CORRUPTION: any change to the numeric value,
               including rounding to fewer significant digits, however small
               the visible difference looks.
  datetime   -> RFC3339 UTC text, fixed 6 fractional digits. LEGITIMATE:
               timezone conversion to UTC (the INSTANT is invariant);
               microsecond padding to 6 digits. CORRUPTION: any change to
               the instant represented (including silent truncation of
               sub-microsecond info, which cannot happen here because Python
               datetime has no finer resolution -- but IS the general
               failure mode this shape guards against).
  list/tuple -> JSON array, ORDER PRESERVED. LEGITIMATE: none -- order is
               semantic. CORRUPTION: reordering, dropping, or duplicating
               elements.
  Mapping    -> JSON object, KEYS SORTED. LEGITIMATE: key reordering (an
               explicit, documented transformation -- two mappings that
               differ only in original key order must digest identically).
               CORRUPTION: a value change under any key, a key added or
               removed, OR two distinct admitted keys silently colliding
               onto the same JSON key (data loss disguised as success).

Given that contract, the CORE PROPERTY under test for the whole encoder is:

    non_canonical_reason(x) is None
        implies
    parse_canonical(canonical_bytes(x)) is semantically equivalent to x
    under the contract above, AND assert_fixpoint(x) succeeds.

Every "known defect" test below is a case where that implication is FALSE
today. They are written as explicit reproductions (pinned, not xfail) where
the defect is deterministic and always fires, and as `xfail(strict=True)`
generative property tests where the defect fires probabilistically across a
seeded corpus -- so a future fix flips them to an unexpected pass, which
pytest reports as a failure demanding the xfail marker be removed.
"""

from __future__ import annotations

import decimal
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.realtime import archive_head as ah
from app.realtime import canonical as cn
from app.realtime import segment as sg

from tests.harness_encoder_fidelity import seeded_gen as gen
from tests.harness_encoder_fidelity import reference_and_broken as rb
from tests.harness_encoder_fidelity.subprocess_timeout import run_script

UTC = timezone.utc
REPO_ROOT = str(Path(__file__).resolve().parents[1])

EXHAUSTIVE = os.environ.get("KALSHI_HARNESS_EXHAUSTIVE") == "1"
CORPUS_N = 400 if EXHAUSTIVE else 60
CORPUS_BASE_SEED = 20260809           # today's date, arbitrary but fixed


def _init_archive(root, environment="demo"):
    try:
        ah.initialize_archive(Path(root), environment,
                              archive_identity="kalshi-realtime")
    except ah.ArchiveHeadError:
        pass
    return root


def _fields(i, raw_event, ts=None):
    ts = ts or datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    return {
        "connection_generation": 1, "subscription_id": 4,
        "subscription_generation": 1, "message_type": "orderbook_delta",
        "market_ticker": "KXA", "seq": i,
        "received_at_utc": cn.canonical_datetime(ts),
        "received_monotonic_ns": 1_000_000 + i,
        "raw_event": raw_event,
        "normalized_event": {"venue_side": "no", "raw_price_units": 5100},
    }


# =====================================================================
# 1. THE KNOWN DEFECT, REPRODUCED EXACTLY: canonical_decimal() silently
#    corrupts ordinary venue values whose significant-digit count exceeds
#    the AMBIENT decimal context (default prec=28), because
#    canonical.py:119 calls value.normalize() with no explicit context,
#    while admission (segment.py:371, _MAX_DECIMAL_DIGITS=512) advertises
#    a much larger capability. Every value below is well inside the
#    admitted 512-digit envelope.
# =====================================================================

class TestKnownDefectDecimalCorruption:
    """Reproduces, does not fix. See canonical.py line ~119."""

    EXACT_COUNTEREXAMPLES = [
        # (input text, forbidden output -- the corruption the task names)
        ("1.000000000000000000000000000000001", "1"),
        ("9" * 40, str(10 ** 40)),
        ("12345678901234567890123456789012345",
         "12345678901234567890123456790000000"),
    ]

    @pytest.mark.parametrize("raw,forbidden", EXACT_COUNTEREXAMPLES)
    def test_ordinary_value_is_silently_changed(self, raw, forbidden):
        d = Decimal(raw)
        text = cn.canonical_decimal(d)
        assert text == forbidden, (
            f"expected the KNOWN corruption {forbidden!r}, got {text!r} -- "
            "either the defect no longer reproduces (good -- update this "
            "harness) or reproduces differently than documented")
        assert Decimal(text) != d, (
            "the corrupted text happens to still equal the original value; "
            "this specific input no longer demonstrates the defect")

    @pytest.mark.parametrize("raw,_forbidden", EXACT_COUNTEREXAMPLES)
    def test_admission_reports_ADMITTED_none_canonical_reason_is_none(
            self, raw, _forbidden):
        """THE INVARIANT non_canonical_reason claims to hold --
        `non_canonical_reason(x) is None` implies `canonical_bytes(x)`
        succeeds -- holds here. What it does NOT claim, and what is false,
        is that canonical_bytes(x) preserves x's value."""
        d = Decimal(raw)
        assert sg.non_canonical_reason({"price": d}) is None

    @pytest.mark.parametrize("raw,forbidden", EXACT_COUNTEREXAMPLES)
    def test_digest_is_taken_over_the_corrupted_text(self, raw, forbidden):
        d = Decimal(raw)
        digest_of_corrupted_value = cn.digest_hex({"price": d})
        digest_of_forbidden_text = cn.digest_hex({"price": forbidden})
        # canonical_decimal(d) == forbidden (proven above), and canonical
        # encoding of a Decimal IS that text, so the record's digest is
        # identical to the digest of the WRONG value re-submitted as a str
        # everywhere the encoder would see a Decimal equal to `forbidden`.
        assert digest_of_corrupted_value == cn.digest_hex(
            {"price": cn.canonical_decimal(d)})
        assert digest_of_corrupted_value == digest_of_forbidden_text

    @pytest.mark.parametrize("raw,forbidden", EXACT_COUNTEREXAMPLES)
    def test_verify_chain_reports_VALID_over_the_corrupted_value(
            self, raw, forbidden, tmp_path):
        """End-to-end: write a record carrying the corrupting Decimal through
        the real writer, close the segment, and show verify_chain /
        verify_segment / verify_archive all report VALID. The corruption
        happened BEFORE the digest was computed, so nothing downstream --
        not the chain, not the manifest, not the archive head -- can ever
        see it. This is what "undetectable after the fact" means."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-decimal-defect",
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata={})
        original = Decimal(raw)
        assert w.submit(_fields(0, {"price": original})) is None
        manifest = w.close()
        assert manifest["close_status"] == "clean"

        records = sg.read_segment_records(w.events_path)
        archived_price_text = records[0]["raw_event"]["price"]
        assert archived_price_text == forbidden, (
            "the archive did not store the corrupted text; the defect did "
            "not reproduce through the real writer path")
        assert Decimal(archived_price_text) != original

        chain = sg.verify_chain(records, segment_id="seg-decimal-defect",
                                environment="demo")
        assert chain.ok, chain.reason
        segv = sg.verify_segment(w.dir, environment="demo")
        assert segv.valid, segv.reasons
        archv = sg.verify_archive(tmp_path, environment="demo")
        assert archv["verdict"] == "VALID", archv

    def test_producer_instructed_path_reproduces_it(self):
        """The module docstring instructs producers to reach Decimal via
        `json.loads(wire, parse_float=Decimal)`. Confirm the defect
        reproduces along that exact path, not just via Decimal(str(...))."""
        import json
        wire = '{"price": 1.000000000000000000000000000000001}'
        parsed = json.loads(wire, parse_float=Decimal)
        assert isinstance(parsed["price"], Decimal)
        text = cn.canonical_decimal(parsed["price"])
        assert text == "1"
        assert Decimal(text) != parsed["price"]


# =====================================================================
# 2. THE CORE PROPERTY, GENERATIVE / SEEDED: for every ADMITTED Decimal d,
#    Decimal(canonical_decimal(d)) == d EXACTLY. Hypothesis is not
#    installed (`import hypothesis` -> ModuleNotFoundError under this
#    repo's venv, checked at harness-build time); generation is
#    deterministic and seeded instead. Every failure prints its seed.
# =====================================================================

class TestCoreDecimalFidelityProperty:
    def test_within_ambient_precision_holds_as_a_true_property(self):
        """Decimals with <=27 significant digits never exercise the ambient
        28-digit boundary, and the property genuinely holds here -- this
        must stay green."""
        failures = []
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED):
            rng = gen.make_rng(seed)
            d = gen.gen_small_decimal(rng)
            assert sg.non_canonical_reason({"x": d}) is None, seed
            if not rb.decimal_fidelity_holds(d, cn.canonical_decimal):
                failures.append((seed, d))
        assert not failures, (
            f"fidelity violated for {len(failures)} in-precision seeds "
            f"(unexpected): {failures[:5]}")

    @pytest.mark.xfail(
        strict=True, reason=(
            "KNOWN DEFECT (canonical.py ~line 119): canonical_decimal() "
            "normalizes in the AMBIENT decimal context (prec=28) instead of "
            "a context sized to the value, so any admitted Decimal with "
            ">28 significant digits (up to the admitted 512-digit bound) is "
            "silently rounded. This xfail is the regression trap: it must "
            "start FAILING (i.e. flip to an unexpected pass) the day this "
            "is fixed, at which point delete the marker."))
    def test_beyond_ambient_precision_is_the_known_defect(self):
        failures = []
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED + 100_000):
            rng = gen.make_rng(seed)
            d = gen.gen_large_decimal(rng)
            assert sg.non_canonical_reason({"x": d}) is None, (
                f"seed {seed} was refused at admission; expected ADMITTED "
                f"(this would mean the corpus generated an out-of-bound "
                f"value, not the defect) -- d={d!r}")
            if not rb.decimal_fidelity_holds(d, cn.canonical_decimal):
                failures.append((seed, d))
        assert not failures, (
            f"(expected-to-fail assertion) fidelity held for a >28-digit "
            f"corpus of {CORPUS_N} seeds -- listing the (seed, value) pairs "
            f"that DID corrupt so the failure is reproducible: "
            f"{failures[:10]}")


# =====================================================================
# 3. CAPABILITY DEFINITION: admission and the encoder must be a single
#    source of truth. segment.py:_MAX_DECIMAL_DIGITS=512 claims a
#    capability the encoder (ambient prec=28) does not have. This drift IS
#    the defect above, restated as a capability-contract test.
# =====================================================================

class TestSingleSourceOfTruthCapability:
    def test_ambient_decimal_context_matches_the_advertised_capability(self):
        """Documents the drift directly: the admission gate's own constant
        (_MAX_DECIMAL_DIGITS) and the encoder's actual working precision
        (decimal.getcontext().prec, what normalize() uses with no explicit
        context) are two numbers that can independently drift, which is
        exactly the anti-pattern the rest of this milestone's commits were
        written to eliminate for every OTHER constant in this file."""
        advertised = sg._MAX_DECIMAL_DIGITS
        actual_ambient_precision = decimal.getcontext().prec
        assert advertised == 512
        assert actual_ambient_precision == 28
        assert advertised != actual_ambient_precision, (
            "if this ever becomes equal it is by accident of a changed "
            "ambient context, not by a fix -- see the xfail below for the "
            "real regression trap")

    @pytest.mark.xfail(
        strict=True, reason=(
            "the DEFINITION test: _MAX_DECIMAL_DIGITS (512, segment.py) and "
            "the encoder's real capability (decimal.getcontext().prec, 28, "
            "canonical.py) are not one source of truth. This must fail "
            "today and start passing only once admission and the encoder "
            "share the same constant (e.g. the encoder runs inside a "
            "localcontext sized from _MAX_DECIMAL_DIGITS)."))
    def test_admission_capability_equals_encoder_capability(self):
        max_digits = sg._MAX_DECIMAL_DIGITS
        digits = "7" * max_digits
        d = Decimal(digits)
        assert sg.non_canonical_reason({"x": d}) is None
        assert rb.decimal_fidelity_holds(d, cn.canonical_decimal), (
            "a Decimal at exactly the admitted digit ceiling must survive "
            "the encoder unchanged if the two limits were actually one "
            "source of truth")


# =====================================================================
# 4. THE WHOLE-OBJECT FIDELITY CONTRACT, GENERALISED BEYOND DECIMAL. See the
#    module docstring above for the exact per-type contract this enforces.
# =====================================================================

def _semantically_equivalent(original, decoded, path="value"):
    """Checks `decoded` against `original` under the contract stated in the
    module docstring. Returns (ok, reason)."""
    if isinstance(original, bool):
        if decoded is not True and decoded is not False:
            return False, f"{path}: bool became {decoded!r}"
        return (decoded == original), f"{path}: bool value changed"
    if original is None:
        return (decoded is None), f"{path}: None became {decoded!r}"
    if isinstance(original, int):
        return (decoded == original and isinstance(decoded, int)
                and not isinstance(decoded, bool)), f"{path}: int changed"
    if isinstance(original, Decimal):
        if not isinstance(decoded, str) or not cn.is_canonical_decimal_text(decoded):
            return False, f"{path}: Decimal did not become canonical text"
        return (Decimal(decoded) == original), (
            f"{path}: Decimal value changed ({original!r} -> {decoded!r})")
    if isinstance(original, datetime):
        if not isinstance(decoded, str):
            return False, f"{path}: datetime did not become str"
        try:
            back = cn.parse_canonical_datetime(decoded)
        except cn.CanonicalError as exc:
            return False, f"{path}: datetime text not parseable: {exc}"
        return (back == original.astimezone(UTC)), f"{path}: instant changed"
    if isinstance(original, str):
        return (decoded == original), f"{path}: str changed"
    from collections.abc import Sequence as _SequenceABC

    if isinstance(original, (list, tuple)) or (
            isinstance(original, _SequenceABC)
            and not isinstance(original, (str, bytes))):
        # Any admitted Sequence (list/tuple, or an ABC-registered
        # look-alike) -- iterate it the same way the encoder does.
        original_items = list(original)
        if not isinstance(decoded, list) or len(decoded) != len(original_items):
            return False, f"{path}: list shape changed"
        for i, (ov, dv) in enumerate(zip(original_items, decoded)):
            ok, reason = _semantically_equivalent(ov, dv, f"{path}[{i}]")
            if not ok:
                return False, reason
        return True, ""
    if isinstance(original, dict):
        if not isinstance(decoded, dict) or len(decoded) != len(original):
            return False, f"{path}: mapping key count changed (collision?)"
        for k, ov in original.items():
            if k not in decoded:
                return False, f"{path}: key {k!r} lost"
            ok, reason = _semantically_equivalent(ov, decoded[k], f"{path}.{k}")
            if not ok:
                return False, reason
        return True, ""
    raise AssertionError(f"generator produced an untyped value: {original!r}")


def _check_fidelity(value):
    """The whole-object CORE PROPERTY. Raises AssertionError with a precise
    reason on the first violation."""
    reason = sg.non_canonical_reason(value)
    assert reason is None, f"generator produced a non-admitted value: {reason}"
    encoded = cn.canonical_bytes(value)
    decoded = cn.parse_canonical(encoded)
    ok, why = _semantically_equivalent(value, decoded)
    assert ok, why
    cn.assert_fixpoint(value)   # the second half of the contract: see class 6


class TestWholeObjectFidelityProperty:
    """Generalises TestCoreDecimalFidelityProperty to every admitted
    semantic type and to nested structures. Decimals here are drawn from
    gen_small_decimal (in-precision) so this class stays green -- the
    Decimal-specific defect is isolated and pinned in the classes above."""

    def _small_cfg(self):
        return gen.GenConfig(max_depth=3, max_container_size=5,
                             decimal_digit_range=(1, 20))

    def test_scalars_and_nested_structures(self):
        cfg = self._small_cfg()
        failures = []
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED + 200_000):
            rng = gen.make_rng(seed)
            value = gen.gen_value(rng, cfg)
            try:
                _check_fidelity(value)
            except AssertionError as exc:
                failures.append((seed, str(exc)[:200]))
        assert not failures, (
            f"{len(failures)}/{CORPUS_N} seeds violated fidelity: "
            f"{failures[:5]}")

    def test_mapping_key_order_is_the_one_legitimate_reordering(self):
        a = {"z": 1, "a": Decimal("2"), "m": [1, 2, 3]}
        b = {"a": Decimal("2"), "m": [1, 2, 3], "z": 1}
        assert cn.canonical_bytes(a) == cn.canonical_bytes(b)

    def test_list_order_is_never_legitimately_changed(self):
        a = cn.canonical_bytes([1, 2, 3])
        b = cn.canonical_bytes([3, 2, 1])
        assert a != b

    def test_bool_never_collapses_onto_int(self):
        assert cn.canonical_bytes({"x": True}) != cn.canonical_bytes({"x": 1})
        assert cn.canonical_bytes({"x": False}) != cn.canonical_bytes({"x": 0})

    def test_int_at_and_beyond_max_int_bits_boundary(self):
        just_inside = (1 << (sg._MAX_INT_BITS - 1))
        just_outside = (1 << sg._MAX_INT_BITS)
        assert sg.non_canonical_reason({"x": just_inside}) is None
        _check_fidelity({"x": just_inside})
        reason = sg.non_canonical_reason({"x": just_outside})
        assert reason is not None and "bits" in reason

    def test_str_lone_surrogate_is_refused_not_corrupted(self):
        """The contract says a str that cannot be UTF-8 encoded must be
        REFUSED, never silently re-encoded. Confirm admission actually does
        that (it is the correct behaviour -- this is a control, not a
        defect reproduction)."""
        for seed in gen.seeds(20, CORPUS_BASE_SEED + 300_000):
            rng = gen.make_rng(seed)
            bad = gen.gen_lone_surrogate(rng)
            reason = sg.non_canonical_reason({"x": bad})
            assert reason is not None, (
                f"lone surrogate {bad!r} (seed {seed}) was ADMITTED -- "
                "the harness expects this to be refused")

    def test_naive_datetime_is_refused_not_corrupted(self):
        naive = datetime(2026, 8, 8, 12, 0, 0)
        reason = sg.non_canonical_reason({"t": naive})
        assert reason is not None and "naive" in reason

    def test_calendar_extreme_datetimes(self):
        for dt in (datetime(1, 1, 1, tzinfo=UTC),
                  datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)):
            reason = sg.non_canonical_reason({"t": dt})
            assert reason is None
            _check_fidelity({"t": dt})

    def test_hostile_utcoffset_is_refused_not_crashed(self):
        hostile = datetime(2026, 8, 8, tzinfo=gen.HostileUtcOffset())
        reason = sg.non_canonical_reason({"t": hostile})
        assert reason is not None and "utcoffset" in reason

    def test_datetime_overflowing_astimezone_is_refused_not_crashed(self):
        """datetime.max with a NEGATIVE-offset tzinfo (local time behind
        UTC, so converting to UTC adds hours) overflows the calendar on
        astimezone(); this must be a refusal, per the canonical.py
        docstring, not an ArithmeticError/OverflowError escaping the
        caller. (datetime.min with a POSITIVE offset is the symmetric
        case, checked below.)"""
        overflow_tz = timezone(timedelta(hours=-14))
        hostile = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=overflow_tz)
        reason = sg.non_canonical_reason({"t": hostile})
        assert reason is not None

        underflow_tz = timezone(timedelta(hours=14))
        hostile_min = datetime(1, 1, 1, 0, 0, 0, 0, tzinfo=underflow_tz)
        reason_min = sg.non_canonical_reason({"t": hostile_min})
        assert reason_min is not None


# =====================================================================
# 5. THE LIVE-REFERENCE MUTATION DEFECT (reproduced, not fixed).
#    submit() queues the caller's live dict; the writer encodes it LATER,
#    on its own thread. An ordinary producer that mutates the dict after
#    submit() returns changes what gets archived -- with NO hostile class
#    and, in the common case, NO error at all.
# =====================================================================

class TestLiveReferenceMutationDefect:
    def test_ordinary_mutation_after_submit_silently_corrupts_the_archive(
            self, tmp_path):
        """payload = {"depth": [{"price": "0.51"}]}; submit(); then mutate
        payload in place; then close(). close_status is "clean" and the
        archived bytes do NOT represent the state submit() accepted."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-live-mutation",
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata={})
        # Widen the window between admission and the writer thread actually
        # encoding the record, so the race is deterministic in a test
        # rather than a timing coincidence.
        w.pre_write_hook = lambda writer: time.sleep(0.2)

        payload = {"depth": [{"price": "0.51"}]}
        accepted_snapshot = {"depth": [{"price": "0.51"}]}   # deep copy, for proof
        assert w.submit(_fields(0, payload)) is None
        payload["depth"][0]["price"] = "0.99"     # ordinary dict, no hostile class

        manifest = w.close()
        assert manifest["close_status"] == "clean"

        records = sg.read_segment_records(w.events_path)
        archived_price = records[0]["raw_event"]["depth"][0]["price"]
        assert archived_price != accepted_snapshot["depth"][0]["price"], (
            "expected the KNOWN defect: archived value should differ from "
            "what submit() accepted")
        assert archived_price == "0.99"
        # And the segment verifies VALID regardless -- the mutation is
        # invisible to every downstream check, because the writer only ever
        # saw the post-mutation state.
        segv = sg.verify_segment(w.dir, environment="demo")
        assert segv.valid, segv.reasons

    def test_mutation_introducing_a_refused_type_destroys_the_whole_segment(
            self, tmp_path):
        """The more violent variant: the post-submit mutation introduces a
        float. The writer thread's canonical_bytes() call fails,
        failed_after_accept becomes nonzero, and close() refuses to publish
        -- destroying every OTHER record in the segment along with it."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-live-mutation-float",
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata={})
        w.pre_write_hook = lambda writer: time.sleep(0.2)
        payload = {"depth": [{"price": "0.51"}]}
        assert w.submit(_fields(0, payload)) is None
        payload["depth"][0]["price"] = 0.52     # float -- not caught at admission
        with pytest.raises(sg.SegmentError, match="not written"):
            w.close()


# =====================================================================
# 6. ADMISSION CHECKS canonical_bytes() SUCCEEDS BUT NOT assert_fixpoint().
#    Property test + the known counterexample: a str subclass with an
#    overridden __hash__/__eq__ that produces duplicate JSON keys.
# =====================================================================

class _CollidingKey(str):
    """A str subclass whose __hash__ is constant and whose __eq__ always
    disagrees -- so two DISTINCT dict keys both compare unequal to every
    other key (including each other) yet, once handed to json.dumps, both
    serialise to the SAME JSON text. Nothing hostile happens at the dict
    level: Python's own dict considers _CollidingKey("a") and
    _CollidingKey("a") to be TWO keys, because __eq__ says they differ."""

    def __hash__(self):
        return 0

    def __eq__(self, other):
        return False


class TestAssertFixpointGap:
    def test_property_holds_for_ordinary_random_mappings(self):
        """With real (non-hostile) str keys, natural hash collisions of this
        kind essentially never occur, so this should -- and does -- hold as
        a genuine property across the corpus."""
        cfg = gen.GenConfig(max_depth=2, max_container_size=6)
        failures = []
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED + 400_000):
            rng = gen.make_rng(seed)
            value = gen.gen_value(rng, cfg)
            reason = sg.non_canonical_reason(value)
            if reason is not None:
                continue
            try:
                cn.assert_fixpoint(value)
            except cn.CanonicalError as exc:
                failures.append((seed, str(exc)))
        assert not failures, failures[:5]

    def test_admission_says_canonical_bytes_succeeds(self):
        """Confirms admission's actual precondition: it asks the encoder,
        not assert_fixpoint."""
        d = {_CollidingKey("a"): 1, _CollidingKey("a"): 2}
        assert len(d) == 2, "the dict itself must hold two distinct keys"
        assert sg.non_canonical_reason(d) is None
        # canonical_bytes() genuinely does succeed -- it is not a crash.
        encoded = cn.canonical_bytes(d)
        assert encoded.count(b'"a"') == 2, encoded    # both keys present, duplicated

    @pytest.mark.xfail(
        strict=True, reason=(
            "KNOWN GAP: non_canonical_reason() (segment.py) checks that "
            "canonical_bytes(x) SUCCEEDS but never checks assert_fixpoint(x). "
            "A str subclass with a hash/eq pair that produces duplicate JSON "
            "keys is ADMITTED even though re-parsing its canonical bytes "
            "silently drops one of the two values (json.loads keeps only the "
            "LAST occurrence of a duplicate key) -- so admission's own "
            "invariant, 'ADMITTED implies representable', is true by the "
            "letter (it IS representable) while being false in spirit (what "
            "gets archived is not what was submitted). This must fail today "
            "and start passing once admission also requires assert_fixpoint."))
    def test_admitted_implies_fixpoint(self):
        d = {_CollidingKey("a"): 1, _CollidingKey("a"): 2}
        assert sg.non_canonical_reason(d) is None
        cn.assert_fixpoint(d)   # raises CanonicalError today -- that's the gap

    def test_the_duplicate_key_collapse_destroys_the_whole_segment_at_close(
            self, tmp_path):
        """Not xfail: pins the ACTUAL consequence through the real writer,
        which turns out to be worse than silent data loss on one record.

        admission ADMITS the colliding-key payload (proven above); the
        writer thread writes a line whose record_digest was computed over
        the IN-MEMORY record (raw_event still has two distinct dict keys,
        so its JSON text carries a duplicate "a" key and hashes one way).
        When close() reads that line back to reconcile
        (`read_segment_records` -> `parse_canonical` -> `json.loads`), the
        duplicate JSON key collapses to ONE key (last-wins) -- the record
        parsed back is no longer byte-identical to what was digested, its
        recomputed self-digest disagrees with the stored one, and
        `verify_chain` reports the record as failing its own digest.
        `close()` then refuses to publish -- destroying every OTHER record
        in the segment along with this one, over a value admission should
        never have accepted in the first place."""
        _init_archive(tmp_path)
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-colliding-key",
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata={})
        good_payload = {"price": "0.51"}
        assert w.submit(_fields(0, good_payload)) is None   # a perfectly good record
        colliding_payload = {_CollidingKey("a"): "first", _CollidingKey("a"): "second"}
        assert w.submit(_fields(1, colliding_payload)) is None   # ADMITTED (the gap)
        with pytest.raises(sg.SegmentError, match="fails its own digest"):
            w.close()
        # The good record at ordinal 0 is destroyed too -- close() never
        # published a manifest, so nothing about this segment is evidence.
        assert not w.manifest_path.exists()


# =====================================================================
# 7. TERMINATION LIVES INSIDE THE ENCODER, NOT ONLY IN THE ADMISSION WALK.
#    Every uncertain-termination scenario runs in a SUBPROCESS with an
#    EXTERNAL wall-clock timeout enforced by the parent, because an
#    in-process deadline cannot prove a hang.
# =====================================================================

_HANG_TIMEOUT_S = 3.0


class TestTerminationInsideEncoder:
    def test_bounded_sequence_walk_refuses_an_infinite_sequence(self):
        """_MAX_SEQUENCE_ELEMENTS (segment.py:441) DOES bound the Sequence
        branch of the admission walk. Control case: must terminate quickly
        with a refusal, not a timeout."""
        body = """
from collections.abc import Sequence
from app.realtime import segment as sg

class InfiniteSequence(Sequence):
    def __getitem__(self, i):
        return 1
    def __len__(self):
        return 10**9

r = sg.non_canonical_reason(InfiniteSequence())
print("REFUSED:" + str(r) if r is not None else "RESULT:ADMITTED-unexpected")
"""
        v = run_script(body, timeout_s=15, repo_root=REPO_ROOT)
        assert v.classification == "REFUSED", v
        assert v.duration_s < 10, (
            f"the bounded walk took {v.duration_s:.2f}s -- slower than "
            "expected but still bounded")

    def test_unbounded_mapping_walk_on_an_infinite_mapping_HANGS(self):
        """KNOWN DEFECT: _MAX_SEQUENCE_ELEMENTS bounds the Sequence branch
        (segment.py:441) but has NO twin on the Mapping branch
        (segment.py:408's `for k, v in value.items()` has no counter at
        all). An admission-time Mapping whose iteration never terminates
        hangs the admission walk forever. This MUST be classified TIMEOUT
        by the parent, never REFUSED or COMPLETED."""
        body = """
from collections.abc import Mapping
from app.realtime import segment as sg

class InfiniteMapping(Mapping):
    def __getitem__(self, k):
        return "v"
    def __len__(self):
        return 10**9
    def __iter__(self):
        i = 0
        while True:
            yield f"k{i}"
            i += 1

r = sg.non_canonical_reason(InfiniteMapping())
print("RESULT:" + str(r))
"""
        v = run_script(body, timeout_s=_HANG_TIMEOUT_S, repo_root=REPO_ROOT)
        assert v.classification == "TIMEOUT", (
            f"expected the admission walk to hang on an infinite Mapping "
            f"(the known asymmetry with the Sequence branch); got "
            f"{v.classification} instead: stdout={v.stdout!r}")

    def test_finite_first_iteration_infinite_second_HANGS_in_the_encoder(
            self):
        """The design runs the encoder INSIDE the gate: non_canonical_reason
        itself walks a candidate value TWICE (a bounded structural fast
        path, then a real canonical_bytes() call). A container that is
        finite on its first two __iter__() calls but infinite on any call
        after that PASSES admission cleanly (both internal walks see the
        finite shape) and then hangs the THIRD walk -- the encoder running
        again later, e.g. in the writer thread at actual write time."""
        body = """
from collections.abc import Sequence
from app.realtime import segment as sg
from app.realtime import canonical as cn

class FiniteFirstTwoPassesInfiniteThird(Sequence):
    def __init__(self):
        self._passes = 0
    def __len__(self):
        return 5
    def __getitem__(self, i):
        raise NotImplementedError
    def __iter__(self):
        self._passes += 1
        finite = (self._passes <= 2)
        i = 0
        while True:
            if finite and i >= 5:
                return
            yield 1
            i += 1

obj = {"depth": FiniteFirstTwoPassesInfiniteThird()}
r = sg.non_canonical_reason(obj)
import sys
sys.stderr.write("ADMISSION:" + str(r) + "\\n")
sys.stderr.flush()
b = cn.canonical_bytes(obj)     # simulates the writer thread's later, separate encode
print("RESULT:" + repr(b))
"""
        v = run_script(body, timeout_s=_HANG_TIMEOUT_S, repo_root=REPO_ROOT)
        assert v.classification == "TIMEOUT", (
            f"expected admission to ADMIT (both internal passes finite) and "
            f"then the standalone canonical_bytes() call to hang on the "
            f"third pass; got {v.classification}: stderr={v.stderr!r}")
        assert "ADMISSION:None" in v.stderr, (
            f"expected admission to have printed ADMITTED before the hang; "
            f"stderr so far: {v.stderr!r}")

    def test_deep_recursion_is_refused_gracefully_by_non_canonical_reason(
            self):
        """non_canonical_reason() wraps its whole body in a broad `except
        Exception`, so a RecursionError raised deep inside the structural
        walk becomes a returned reason, not a crash. Control case (no
        defect) -- verified in-process since it does not hang."""
        value = 1
        for _ in range(3000):
            value = [value]
        reason = sg.non_canonical_reason(value)
        assert reason is not None and "RecursionError" in reason

    def test_deep_recursion_escapes_close_as_a_BARE_RecursionError(
            self, tmp_path):
        """KNOWN DEFECT: subscription_metadata (a SegmentWriter constructor
        argument, never passed through non_canonical_reason at all) is
        digested at close() time via build_manifest -> digest_hex ->
        canonical_bytes with NO recursion guard and no broad except above
        it. Deep nesting there escapes close() as a bare RecursionError
        instead of the SegmentError every other close()-time failure is
        normalised into."""
        _init_archive(tmp_path)
        deeply_nested = {"x": 1}
        for _ in range(3000):
            deeply_nested = {"n": deeply_nested}
        w = sg.SegmentWriter(tmp_path, environment="demo",
                             segment_id="seg-deep-recursion",
                             partition_identity="date=2026-08-08/hour=12",
                             subscription_metadata=deeply_nested)
        assert w.submit(_fields(0, {"price": "0.51"})) is None
        with pytest.raises(RecursionError):
            w.close()

    @pytest.mark.skipif(not EXHAUSTIVE, reason=(
        "1,000,000-element walk; run with KALSHI_HARNESS_EXHAUSTIVE=1 for "
        "the full corpus. A smaller bound is exercised by the boundary "
        "test below to keep default runtime sane."))
    def test_very_wide_sequence_at_the_bound_is_refused(self):
        reason = sg.non_canonical_reason(
            {"x": list(range(sg._MAX_SEQUENCE_ELEMENTS + 1))})
        assert reason is not None and "elements" in reason

    def test_sequence_boundary_is_enforced_at_a_smaller_scale(self):
        """Same assertion, a scale that keeps default runtime sane: replaces
        list length with the constant itself via a lazy Sequence, so we
        don't have to materialise a million-element Python list by default.
        """
        from collections.abc import Sequence

        class BoundaryProbe(Sequence):
            def __init__(self, n):
                self._n = n
            def __len__(self):
                return self._n
            def __getitem__(self, i):
                if i >= self._n:
                    raise IndexError
                return 1

        at_bound = BoundaryProbe(sg._MAX_SEQUENCE_ELEMENTS)
        over_bound = BoundaryProbe(sg._MAX_SEQUENCE_ELEMENTS + 1)
        assert sg.non_canonical_reason({"x": at_bound}) is None
        reason = sg.non_canonical_reason({"x": over_bound})
        assert reason is not None and "elements" in reason

    def test_hostile_items_override_is_a_controlled_refusal(self):
        """A Mapping whose .items() itself raises. Confirms the walk turns
        this into a returned reason (not a crash) -- a control, not a
        defect: verified in-process since it terminates immediately."""
        from collections.abc import Mapping

        class HostileItems(Mapping):
            def __getitem__(self, k):
                return "v"
            def __len__(self):
                return 1
            def __iter__(self):
                return iter(["a"])
            def items(self):
                raise RuntimeError("hostile items")

        reason = sg.non_canonical_reason(HostileItems())
        assert reason is not None and "RuntimeError" in reason

    def test_hostile_getitem_raising_a_non_stopping_exception(self):
        """A Sequence whose __getitem__ raises something other than
        IndexError/StopIteration -- must surface as a refusal, not crash the
        producer's call to submit()."""
        from collections.abc import Sequence

        class HostileGetItem(Sequence):
            def __getitem__(self, i):
                raise ValueError("not IndexError")
            def __len__(self):
                return 5

        reason = sg.non_canonical_reason(HostileGetItem())
        assert reason is not None and "ValueError" in reason

    def test_hostile_hash_and_eq_are_a_controlled_refusal_or_admission(
            self):
        """__hash__/__eq__ overrides on a mapping KEY do not crash the walk
        either way (they are the mechanism behind the assert_fixpoint gap
        above, covered separately) -- confirms no interpreter-level crash."""
        d = {_CollidingKey("a"): 1, _CollidingKey("b"): 2}
        # Must not raise here; behaviour (admit vs refuse) is covered above.
        sg.non_canonical_reason(d)

    def test_abc_registered_container_is_walked_like_a_native_one(self):
        """A plain class explicitly registered as a Sequence via
        collections.abc, rather than subclassing it -- confirms the walk
        dispatches on the registration, not on isinstance-of-a-concrete-base
        alone, and stays bounded."""
        from collections.abc import Sequence

        class NotReallyASequence:
            def __init__(self, items):
                self._items = items
            def __getitem__(self, i):
                return self._items[i]
            def __len__(self):
                return len(self._items)

        Sequence.register(NotReallyASequence)
        obj = NotReallyASequence([1, 2, 3])
        assert isinstance(obj, Sequence)
        assert sg.non_canonical_reason({"x": obj}) is None
        _check_fidelity({"x": obj})


# =====================================================================
# 8. DISCRIMINATION: (a) a known-good control passes, (b) deliberately
#    broken stand-ins are caught, (c) the external timeout mechanism itself
#    is proven against a control that must never terminate.
# =====================================================================

class TestDiscrimination:
    def test_a_known_good_reference_decimal_canonicalizer_satisfies_fidelity(
            self):
        """(a) Positive control: a CORRECT implementation (normalize() in a
        localcontext sized from the value, never the ambient/default
        context) satisfies the exact-equality property across the same
        large-digit corpus that falsifies production's implementation."""
        failures = []
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED + 500_000):
            rng = gen.make_rng(seed)
            d = gen.gen_large_decimal(rng)
            if not rb.decimal_fidelity_holds(d, rb.reference_canonical_decimal):
                failures.append((seed, d))
        assert not failures, (
            f"reference implementation failed for {len(failures)} seeds -- "
            f"the reference itself is broken, which would undermine every "
            f"other assertion in this file: {failures[:5]}")

    @pytest.mark.parametrize("name,broken_fn", [
        ("ambient_precision (== production's own defect, standalone)",
         rb.broken_ambient_precision_decimal),
        ("float_str_round_trip", rb.broken_float_str_decimal),
        ("fixed_28_digit_truncation", rb.broken_truncating_decimal),
    ])
    def test_known_bad_stand_ins_are_all_caught(self, name, broken_fn):
        """(b) Negative controls: three DIFFERENT deliberately-broken
        encoders, none of them app.realtime.canonical itself, must all be
        caught by decimal_fidelity_holds() -- proving the property check has
        teeth rather than trivially passing everything."""
        caught = 0
        checked = 0
        for seed in gen.seeds(CORPUS_N, CORPUS_BASE_SEED + 600_000):
            rng = gen.make_rng(seed)
            d = gen.gen_large_decimal(rng)
            checked += 1
            if not rb.decimal_fidelity_holds(d, broken_fn):
                caught += 1
        assert caught > 0, (
            f"{name}: the harness caught ZERO violations out of {checked} "
            f"large-digit seeds -- this stand-in is not actually broken for "
            f"this corpus, which means it is not a useful negative control")

    def test_external_timeout_is_proven_against_a_true_non_terminator(
            self):
        """(c) The external-timeout mechanism itself is only trustworthy if
        it can correctly report a genuine infinite loop as TIMEOUT and never
        as a pass. This control never calls into app.realtime at all -- it
        is a bare `while True: pass` -- and exists purely to prove the
        parent's kill-and-classify path works before any of the encoder
        hang tests above are trusted."""
        body = "\nwhile True:\n    pass\n"
        v = run_script(body, timeout_s=1.5, repo_root=REPO_ROOT)
        assert v.classification == "TIMEOUT", (
            f"a bare infinite loop must ALWAYS be classified TIMEOUT; got "
            f"{v.classification} -- the timeout mechanism itself cannot be "
            f"trusted if this fails")
        assert 1.3 <= v.duration_s <= 10, v.duration_s

    def test_a_terminating_control_is_never_misclassified_as_a_timeout(
            self):
        """The complement of the above: a script that finishes instantly
        must not spuriously read as TIMEOUT (would make every REFUSED/
        COMPLETED assertion above meaningless)."""
        body = 'print("RESULT:ok")\n'
        v = run_script(body, timeout_s=5, repo_root=REPO_ROOT)
        assert v.classification == "COMPLETED", v
