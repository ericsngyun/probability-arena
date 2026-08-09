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
    try:
        normalised = value.normalize()
    except ArithmeticError as exc:
        # `normalize()` sat OUTSIDE the guard below, so decimal.Overflow — an
        # ArithmeticError, not a CanonicalError — escaped into the WRITER and,
        # worse, into verify_chain / verify_archive / read_verified. A
        # tamper-evidence path that dies on a tampered numeric literal is
        # fail-open by crash.
        raise CanonicalError(
            f"{value!r} cannot be normalised to canonical decimal text: {exc!r}"
        ) from exc
    # `format(x, "f")` already renders a positive exponent in full — `1E+30`
    # becomes `1000000000000000000000000000000` — so the quantize that used to
    # sit here was redundant AND actively harmful: `quantize` is evaluated in
    # the current decimal context, whose default precision is 28 digits, so any
    # value at or above ~1e28 raised `decimal.InvalidOperation`. That is an
    # ArithmeticError rather than a CanonicalError, so it escaped the writer's
    # handler, killed the writer thread, and destroyed the whole segment over
    # one ordinary venue number.
    try:
        text = format(normalised, "f")
    except ArithmeticError as exc:              # pragma: no cover - defensive
        raise CanonicalError(
            f"{value!r} cannot be rendered as canonical decimal text: {exc!r}"
        ) from exc
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def _encode(value):
    """Map a Python value onto its canonical JSON-representable form."""
    # bool before int: `isinstance(True, int)` is True, and silently writing
    # `1` for `True` would make two distinct values share a digest.
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
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
        return value
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(
                    f"mapping key {k!r} is {type(k).__name__}; canonical keys "
                    "must be strings so ordering is total and unambiguous")
            out[k] = _encode(v)
        return out
    if isinstance(value, (list, tuple)) or (
            isinstance(value, Sequence) and not isinstance(value, (str, bytes))):
        return [_encode(v) for v in value]
    raise CanonicalError(
        f"{type(value).__name__} has no canonical representation; add one "
        "deliberately rather than letting it fall through to repr()")


def canonical_bytes(value) -> bytes:
    """The one canonical byte representation. Everything digests over this."""
    encoded = _encode(value)
    return json.dumps(
        encoded,
        sort_keys=True,               # total, stable key order
        separators=(",", ":"),        # no insignificant whitespace
        ensure_ascii=False,           # UTF-8 below, not \\u escapes
        allow_nan=False,              # NaN/Infinity are not JSON and not values
    ).encode("utf-8")


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
