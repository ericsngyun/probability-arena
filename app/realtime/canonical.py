"""KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 — the one canonical serializer.

Every consumer of a record's bytes goes through here: writing, reading,
digesting, chaining, manifest verification, the ordered-stream digest, and
replay. Parallel serializers that are merely *intended* to agree are what broke
the archive — `_canon` wrote `data_age_ms` as a bare float, `loads_exact` read
it back as `Decimal`, and `_canon` then re-emitted it through `default=str` as a
**quoted string**, so the recomputed digest could never match. Every record
carrying a venue timestamp was discarded as if it had been tampered with, which
on the live wire is nearly the whole stream.

The fix is not a better encoder. It is the **fixpoint property**:

    canonical_bytes(x) == canonical_bytes(parse(canonical_bytes(x)))

Once that holds, a digest computed before a write necessarily equals the digest
recomputed after a read, because both are taken over the same bytes. Type
fidelity across the round trip is explicitly *not* required — and deliberately
not attempted, because tagging types to preserve them is the thing that
introduces a second representation and therefore a second way to disagree.

So the encoding collapses to a small set of stable forms:

    int          -> JSON integer            (exact, self-inverse)
    bool         -> JSON true/false         (checked BEFORE int; bool is an int)
    None         -> null
    str          -> JSON string             (self-inverse)
    Decimal      -> canonical decimal TEXT  (parses back to str, same bytes)
    datetime     -> RFC3339 UTC, 6 dp TEXT  (parses back to str, same bytes)
    list/tuple   -> JSON array
    mapping      -> JSON object, keys sorted

`Decimal` and `datetime` land on `str` after a round trip, and re-encoding that
`str` produces byte-identical output. The value's Python type changes; the
canonical form does not. That is the only invariant a digest can rest on.

**`float` is refused outright.** Not converted — refused. A float is how the
original defect entered, and accepting one here would mean the canonical form
depends on repr behaviour that varies with value and platform.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Bumped whenever the byte-level encoding changes. A digest is only comparable
# against another digest produced at the same version, so this belongs in the
# record and in the manifest.
CANONICAL_SCHEMA_VERSION = 1

# RFC3339, UTC, always exactly 6 fractional digits. Fixed precision matters:
# `isoformat()` omits the fraction entirely when microsecond == 0, so two
# timestamps one microsecond apart would serialise to different *shapes* and a
# reader could not know which it was looking at.
_DT_PRECISION = 6
_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

_DECIMAL_TEXT_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


class CapabilityLimits:
    """The ONE canonical capability contract for every bounded structure.

    `_MAX_DECIMAL_DIGITS = 512` used to live only in `segment.py`, while the
    encoder's real working precision (the ambient `decimal` context, 28) was
    an entirely separate number nobody had to keep in sync -- that drift IS
    the defect A2 fixed. This class exists so the same class of drift cannot
    happen again for any OTHER bounded shape: nesting depth, mapping size,
    sequence size, string length, or numeric size. Both admission
    (`segment.py`'s structural walk) and the encoder (`_encode` below) import
    these values rather than declaring their own copies, so a test comparing
    `segment._MAX_SEQUENCE_ELEMENTS is CapabilityLimits.MAX_SEQUENCE_ELEMENTS`
    (or any sibling) fails the moment the two are ever defined independently.

    The encoder enforces every one of these bounds ITSELF, unconditionally --
    not only when reached through admission. A container that iterates
    finitely once (satisfying admission's structural walk) and infinitely on
    every later call passes admission cleanly and then hangs a *standalone*
    `canonical_bytes()` call with no bound in the walk alone to catch it; the
    bound has to live in the encoder to close that gap.
    """

    # 256 is comfortably above any legitimate venue payload's nesting (an
    # existing gate test, test_kalshi_archive_genesis_001.py's
    # TestAdmissionIsBounded, exercises 120 levels as a legitimately admitted
    # depth) and comfortably below where Python's own C-stack recursion limit
    # would intervene (default 1000, and this walk uses only a few frames per
    # level), so a bound here is a deliberate refusal rather than a
    # coincidental RecursionError either way.
    MAX_DEPTH = 256
    MAX_MAPPING_ELEMENTS = 1_000_000
    MAX_SEQUENCE_ELEMENTS = 1_000_000
    MAX_STRING_LENGTH = 1_000_000
    MAX_DECIMAL_EXPONENT = 4096
    MAX_DECIMAL_DIGITS = 512
    MAX_INT_BITS = 8192

    # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect B: every bound above is a
    # PER-CONTAINER limit -- it bounds one mapping's element count, one
    # sequence's element count, one nesting chain's depth. None of them bound
    # the TOTAL work a single `canonical_bytes`/admission-walk call can do.
    # `x = 0; for _ in range(60): x = [x, x]` is legal on every one of those
    # axes at every level (depth 60 < 256, width 2 < 1,000,000 per list) and
    # still visits 2**60 nodes, because the same two-element list is SHARED
    # and re-walked from both parents at every level -- a purely structural
    # (not cyclic) blowup local per-container limits cannot see, because each
    # one only ever looks at the container immediately in front of it.
    #
    # This bounds the TOTAL NUMBER OF NODES VISITED by one canonicalization
    # pass (`_encode`'s recursive calls, one increment per call -- and the
    # admission walk's equivalent, `segment._structural_reason`, mirrors it
    # exactly for the same reason: an admitted-then-hangs-on-encode value is
    # exactly as much a defect as a hangs-on-admission one). It does NOT
    # bound output bytes or wall-clock time directly -- it bounds the one
    # quantity that is cheap to count exactly, on every call, with no
    # trust in any container's `__len__`: how many times the encoder (or
    # the admission walk) recurses. A single legitimate container can
    # already visit up to 1,000,000 nodes (`MAX_MAPPING_ELEMENTS`/
    # `MAX_SEQUENCE_ELEMENTS`); this is set well above that so ordinary
    # deeply-nested-but-finite venue payloads are never refused, and far
    # below where an exponential structural blowup can do meaningful
    # damage before being caught (2**21 already exceeds it, so the
    # `range(60)` doubling case above is refused at depth ~21, not 60).
    MAX_CANONICAL_WORK_UNITS = 4_000_000


class CanonicalError(ValueError):
    """A value that cannot be represented canonically."""


def canonical_datetime(value: datetime) -> str:
    """A timezone-aware UTC datetime as fixed-precision RFC3339 text.

    Naive datetimes are refused rather than assumed UTC: `astimezone()` reads a
    naive value as *local* time, so the same event would canonicalise
    differently on two hosts and its digest would not survive the move.
    """
    if not isinstance(value, datetime):
        raise CanonicalError(f"expected datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalError(
            "datetime must be timezone-aware; a naive value is read as LOCAL "
            "time and would canonicalise differently on different hosts")
    try:
        utc = value.astimezone(timezone.utc)
    except (OverflowError, ValueError, OSError) as exc:
        # `datetime.max` with a negative offset (or `.min` with a positive one)
        # overflows the calendar. OverflowError is not a CanonicalError, so it
        # escaped the writer exactly as decimal.InvalidOperation once did.
        raise CanonicalError(
            f"{value!r} cannot be converted to UTC: {exc!r}") from exc
    return (f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
            f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
            f".{utc.microsecond:06d}Z")


def parse_canonical_datetime(text: str) -> datetime:
    """Inverse of `canonical_datetime`. Refuses anything not in canonical shape."""
    if not isinstance(text, str) or not _DT_RE.match(text):
        raise CanonicalError(
            f"{text!r} is not canonical RFC3339 UTC with {_DT_PRECISION} "
            "fractional digits")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=timezone.utc)


def canonical_decimal(value: Decimal) -> str:
    """A Decimal as canonical text.

    Exponent notation and trailing-zero variation are normalised away, so
    `Decimal("1E+2")`, `Decimal("100")` and `Decimal("100.00")` all reach the
    same bytes. Without that, two numerically equal values would produce two
    digests.
    """
    if not isinstance(value, Decimal):
        raise CanonicalError(f"expected Decimal, got {type(value).__name__}")
    if not value.is_finite():
        raise CanonicalError(f"{value!r} is not finite")
    # CONTEXT-INDEPENDENT by construction: this operates directly on the
    # sign/digits/exponent triple (`as_tuple()`), never on `Decimal.normalize()`
    # or any other operation evaluated in the AMBIENT decimal context. The
    # ambient context's precision (default 28 significant digits) used to be
    # exactly what `normalize()` rounded to, silently, for any admitted value
    # with more digits than that — even though admission
    # (`segment.py:_MAX_DECIMAL_DIGITS`) advertises a 512-digit capability.
    # `int(digit_string)` below is exact for any number of digits: Python ints
    # are arbitrary precision, so parsing decimal digit characters into one
    # never rounds, no matter how large. That is what makes this
    # context-independent — no `decimal` arithmetic of any kind is performed,
    # so there is no context, ambient or explicit, left to round anything.
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        # `is_finite()` above already excludes Infinity ('F') and NaN ('n'/'N')
        # exponents; this is defensive only.
        raise CanonicalError(
            f"{value!r} has a non-integer exponent {exponent!r}")
    # BOUNDED, unconditionally -- the encoder must refuse a value outside the
    # shared capability contract even when called directly, bypassing
    # admission's own copy of this same check (segment.py's structural walk).
    # Before this, the encoder had no ceiling of its own at all: admission
    # advertised a capability the encoder never actually enforced, which is
    # the drift class A2 closed for the ambient-precision case specifically.
    if not (-CapabilityLimits.MAX_DECIMAL_EXPONENT
            <= exponent <= CapabilityLimits.MAX_DECIMAL_EXPONENT):
        raise CanonicalError(
            f"{value!r} has decimal exponent {exponent}, outside the "
            f"canonical range +/-{CapabilityLimits.MAX_DECIMAL_EXPONENT}")
    if len(digits) > CapabilityLimits.MAX_DECIMAL_DIGITS:
        raise CanonicalError(
            f"{value!r} has more than {CapabilityLimits.MAX_DECIMAL_DIGITS} "
            "significant digits")
    coefficient = int("".join(str(d) for d in digits)) if digits else 0
    if coefficient == 0:
        return "0"
    digit_str = str(coefficient)
    if exponent >= 0:
        text = digit_str + ("0" * exponent)
    else:
        point = len(digit_str) + exponent
        if point <= 0:
            text = "0." + ("0" * -point) + digit_str
        else:
            text = digit_str[:point] + "." + digit_str[point:]
        if "." in text:
            text = text.rstrip("0").rstrip(".")
    if sign:
        text = "-" + text
    return text


def _check_str(value: str, what: str = "string") -> str:
    """Bound a str against the shared capability contract before it is used
    as a canonical value or a mapping key. Applied unconditionally in the
    encoder -- not only when reached through admission.

    KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect C: a lone UTF-16 surrogate
    (`"\\ud800"`) is legal JSON and round-trips through `json.loads` as an
    ordinary `str` -- but `str.encode("utf-8")` refuses it with
    `UnicodeEncodeError`, a `ValueError` subclass, NOT a `CanonicalError`.
    Before this, that error surfaced only at `canonical_bytes`'s FINAL
    `.encode("utf-8")` (after `json.dumps(..., ensure_ascii=False)` had
    already happily emitted the raw surrogate into the JSON text), so every
    caller that catches `CanonicalError` specifically (`verify_chain`,
    `verify_segment`, the archive-adopt/archive-recover-head CLI commands)
    saw a raw, untyped crash instead of a refusal. Checking encodability
    HERE, at the one place every str value and mapping key passes through
    before being accepted, converts that `ValueError` into the typed
    canonical failure contract as early as possible -- symmetric with
    `segment._structural_reason`'s pre-acceptance str branch, which already
    checked this on the admission side.
    """
    if len(value) > CapabilityLimits.MAX_STRING_LENGTH:
        raise CanonicalError(
            f"{what} of length {len(value)} exceeds the canonical bound of "
            f"{CapabilityLimits.MAX_STRING_LENGTH} characters")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CanonicalError(
            f"{what} is not UTF-8 encodable ({exc.reason} at {exc.start}); "
            "a lone surrogate is legal JSON but has no canonical byte "
            "representation") from exc
    return value


class WorkBudget:
    """A total, aggregate work counter shared across one whole recursive
    walk -- the fix for defect B (per-container limits do not bound total
    work). One unit is consumed per node VISITED (one `_encode`/
    `_structural_reason` call), regardless of that node's own local
    depth/width, so a purely structural fan-out (the SAME sub-object
    referenced from many parents, at legal per-container depth and width
    everywhere) is caught by the TOTAL, not by any single container's own
    bound. Shared, not re-created per recursive call, so it accumulates
    across the entire walk rather than resetting at every level.
    """

    __slots__ = ("units", "limit")

    def __init__(self, limit: int = CapabilityLimits.MAX_CANONICAL_WORK_UNITS):
        self.units = 0
        self.limit = limit

    def consume(self, path: str = "value") -> None:
        self.units += 1
        if self.units > self.limit:
            raise CanonicalError(
                f"canonicalizing {path} exceeded the total aggregate "
                f"work bound of {self.limit} nodes visited -- a purely "
                "structural blowup (the same sub-value shared and "
                "re-walked from many parents) that no single container's "
                "own depth/width limit can see")


def _encode(value, _depth: int = 0, _budget: WorkBudget | None = None):
    """Map a Python value onto its canonical JSON-representable form.

    BOUNDED on every axis in `CapabilityLimits`, unconditionally -- this
    function is the encoder, and it is the encoder that has to prove
    termination, not merely the admission walk that usually runs before it.
    A container that iterates finitely once (satisfying admission) and
    infinitely on every later call passes admission cleanly and then hangs a
    *standalone* `canonical_bytes()` call otherwise; every recursive branch
    below counts its own iteration and its own depth rather than trusting
    `__len__` (which a hostile container can misreport) or trusting that
    admission already checked.

    `_budget` is the AGGREGATE work counter (defect B): every recursive call
    consumes one unit from the SAME shared `WorkBudget`, so it bounds total
    nodes visited across the whole call, not merely each container's own
    local element count -- see `WorkBudget`'s docstring for why a per-
    container bound alone cannot catch a purely structural fan-out.
    """
    if _budget is None:
        _budget = WorkBudget()
    _budget.consume()
    # bool before int: `isinstance(True, int)` is True, and silently writing
    # `1` for `True` would make two distinct values share a digest.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > CapabilityLimits.MAX_INT_BITS:
            raise CanonicalError(
                f"integer of {value.bit_length()} bits exceeds the "
                f"canonical bound of {CapabilityLimits.MAX_INT_BITS} bits")
        return value
    if isinstance(value, float):
        raise CanonicalError(
            "float is not canonically representable. This is the defect that "
            "made the archive write-only: a float written bare and re-read as "
            "Decimal re-serialises differently, so the digest can never match. "
            "Use an int (e.g. microseconds) or a Decimal.")
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        return canonical_datetime(value)
    if isinstance(value, str):
        return _check_str(value)
    if isinstance(value, Mapping):
        if _depth >= CapabilityLimits.MAX_DEPTH:
            raise CanonicalError(
                f"mapping nesting exceeds the canonical depth bound of "
                f"{CapabilityLimits.MAX_DEPTH}")
        out = {}
        count = 0
        seen_keys: set = set()
        for k, v in value.items():
            count += 1
            if count > CapabilityLimits.MAX_MAPPING_ELEMENTS:
                raise CanonicalError(
                    f"mapping has more than "
                    f"{CapabilityLimits.MAX_MAPPING_ELEMENTS} elements")
            # KALSHI-ARCHIVE-CORE-REMEDIATION-003 defect D: `.items()` is the
            # raw source of truth for a Mapping NOT backed by a Python
            # `dict` -- an ordinary (non-hostile) association-list-backed
            # multimap can present the SAME logical string key twice, with
            # DIFFERENT values, no `__hash__`/`__eq__` override required.
            # `out[k] = ...` below is a plain dict assignment: writing the
            # same key twice silently keeps only the LAST value, with no
            # reason surfaced anywhere -- gate, encode, digest, submit,
            # close, or verify all agreed the value was "accepted" while one
            # entry vanished. POLICY: reject outright, checked here BEFORE
            # the value is even encoded, so this is total over every `.items()`
            # this walk will ever see, not merely the ones a caller thought
            # to test. (Two identical repeats of the SAME key/value pair
            # collapse the same way, but lose nothing distinguishable --
            # this still refuses that case too, since `.items()` presenting
            # a key twice at all is the untrustworthy shape, independent of
            # whether the two values happen to be equal.)
            #
            # Type-checked BEFORE the duplicate check: an unhashable key
            # (e.g. a `dict`) would raise a raw `TypeError` from `k in
            # seen_keys` rather than the typed `CanonicalError` below, and a
            # non-`str` key is refused on its own terms regardless of
            # whether it repeats.
            if not isinstance(k, str):
                raise CanonicalError(
                    f"mapping key {k!r} is {type(k).__name__}; canonical keys "
                    "must be strings so ordering is total and unambiguous")
            if k in seen_keys:
                raise CanonicalError(
                    f"mapping key {k!r} is presented more than once by "
                    "this Mapping's .items(); duplicate logical keys are "
                    "refused rather than silently collapsed to the last "
                    "value")
            seen_keys.add(k)
            _check_str(k, "mapping key")
            out[k] = _encode(v, _depth + 1, _budget)
        return out
    if isinstance(value, (list, tuple)) or (
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))):
        if _depth >= CapabilityLimits.MAX_DEPTH:
            raise CanonicalError(
                f"sequence nesting exceeds the canonical depth bound of "
                f"{CapabilityLimits.MAX_DEPTH}")
        out = []
        for i, v in enumerate(value):
            if i >= CapabilityLimits.MAX_SEQUENCE_ELEMENTS:
                raise CanonicalError(
                    f"sequence has more than "
                    f"{CapabilityLimits.MAX_SEQUENCE_ELEMENTS} elements")
            out.append(_encode(v, _depth + 1, _budget))
        return out
    raise CanonicalError(
        f"{type(value).__name__} has no canonical representation; add one "
        "deliberately rather than letting it fall through to repr()")


def canonical_bytes(value) -> bytes:
    """The one canonical byte representation. Everything digests over this."""
    encoded = _encode(value)
    text = json.dumps(
        encoded,
        sort_keys=True,               # total, stable key order
        separators=(",", ":"),        # no insignificant whitespace
        ensure_ascii=False,           # UTF-8 below, not \\u escapes
        allow_nan=False,              # NaN/Infinity are not JSON and not values
    )
    try:
        return text.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        # Defense in depth, defect C: `_check_str` already refuses a lone
        # surrogate in any str value or mapping key BEFORE it reaches here,
        # so this branch should be unreachable in practice -- but the
        # invariant this module promises ("every invalid canonical input
        # terminates in the typed canonical failure contract") has to hold
        # at THIS boundary too, not merely at the one call site a reviewer
        # happened to patch, or the next str-producing branch added to
        # `_encode` reopens exactly this gap.
        raise CanonicalError(
            f"canonical text is not UTF-8 encodable ({exc.reason} at "
            f"{exc.start}); a lone surrogate has no canonical byte "
            "representation") from exc


def parse_canonical(data: bytes | str):
    """Inverse of `canonical_bytes` up to the fixpoint.

    Numbers are parsed as `Decimal` only when fractional; integers stay `int`.
    Decimal and datetime values arrive as `str`, and re-encoding those strings
    reproduces the same bytes — which is the property digests need.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data, parse_float=Decimal, parse_int=int,
                      parse_constant=_reject_constant)


def _reject_constant(name: str):
    raise CanonicalError(f"{name} is not a canonical value")


def digest_hex(value) -> str:
    """SHA-256 over the canonical bytes."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def assert_fixpoint(value) -> bytes:
    """Prove the round-trip invariant for one value and return its bytes.

    Cheap enough to run on real records in tests, and the single check that
    would have caught the original defect at the moment it was written.
    """
    first = canonical_bytes(value)
    second = canonical_bytes(parse_canonical(first))
    if first != second:
        raise CanonicalError(
            "canonical form is not a fixpoint: "
            f"{first[:120]!r} != {second[:120]!r}")
    return first


def coerce_canonical(value):
    """Make an opaque venue payload canonically representable. Lossless on floats.

    `canonical_bytes` refuses `float` and that refusal is right: a float written
    bare and re-read as `Decimal` re-serialises differently, so the digest could
    never match. But `json.loads` produces a float for every fractional number
    the venue sends, so the refusal fired on ORDINARY traffic — the writer
    caught the error, booked the event as dropped-after-acceptance, and the
    segment still committed as `close_status: "clean"`. The producer was told
    the event was accepted and it never reached the archive.

    Refusing at the digest layer and coercing at the ingress boundary is the
    combination that holds: `repr()` of a Python float round-trips exactly, so
    `Decimal(repr(f))` preserves the value and lands on stable canonical text.
    Only the opaque venue payloads go through here; the pinned envelope columns
    stay strictly typed.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise CanonicalError(
                f"{value!r} is not a value; NaN and Infinity are not JSON and "
                "cannot be evidence")
        return Decimal(repr(value))
    if isinstance(value, Mapping):
        return {k: coerce_canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [coerce_canonical(v) for v in value]
    return value


def is_canonical_decimal_text(text: str) -> bool:
    return isinstance(text, str) and bool(_DECIMAL_TEXT_RE.match(text))


def to_decimal(text: str) -> Decimal:
    """Parse canonical decimal text back to a Decimal when arithmetic is needed."""
    if not is_canonical_decimal_text(text):
        raise CanonicalError(f"{text!r} is not canonical decimal text")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise CanonicalError(f"{text!r} is not parseable") from exc
