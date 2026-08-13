"""Discrimination fixtures: one known-good reference implementation and
several deliberately broken stand-ins, used to prove the harness itself has
teeth (it is not just measuring the shape of app.realtime.canonical).

None of this is imported by, or modifies, app/realtime/canonical.py or
app/realtime/segment.py. It reimplements just the Decimal-text step, and a
couple of whole-value encode/decode pairs, standalone.
"""

from __future__ import annotations

import re
from decimal import Decimal, localcontext

_DECIMAL_TEXT_RE = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")


def reference_canonical_decimal(value: Decimal) -> str:
    """A CORRECT decimal canonicalizer: normalize() inside a localcontext
    whose precision is set from the VALUE ITSELF (never the ambient/default
    context), so no admitted Decimal is ever rounded by the encoder.

    This is offered as the known-good CONTROL for the discrimination
    requirement, and as the fix direction in the report -- it is not wired
    into production.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"not a finite Decimal: {value!r}")
    digits = len(value.as_tuple().digits)
    with localcontext() as ctx:
        # +16 of headroom: normalize() must never need to round to fit.
        ctx.prec = max(digits + 16, 60)
        normalised = value.normalize(context=ctx)
        text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def broken_ambient_precision_decimal(value: Decimal) -> str:
    """Reproduces the PRODUCTION defect standalone, for discrimination
    purposes: normalize() runs in whatever the ambient/default decimal
    context happens to be (prec=28), silently rounding any value with more
    significant digits than that. Byte-for-byte what
    app.realtime.canonical.canonical_decimal does at its defective line.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"not a finite Decimal: {value!r}")
    normalised = value.normalize()          # ambient context -- the defect
    text = format(normalised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-", "-0"):
        text = "0"
    return text


def broken_float_str_decimal(value: Decimal) -> str:
    """A different, more obviously-broken stand-in: round-trips through
    float/repr, which loses precision far more aggressively than the ambient
    28-digit context (IEEE-754 double has ~15-17 significant decimal digits).
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"not a finite Decimal: {value!r}")
    return repr(float(value))


def broken_truncating_decimal(value: Decimal) -> str:
    """A stand-in that just chops to a fixed number of significant digits
    without even rounding -- cruder than production's defect but in the same
    family (silent precision loss with a valid-looking output)."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"not a finite Decimal: {value!r}")
    sign, digits, exponent = value.as_tuple()
    digits = digits[:28] if len(digits) > 28 else digits
    text = "".join(str(d) for d in digits) or "0"
    d = Decimal((sign, tuple(int(c) for c in text), exponent))
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out or "0"


def is_canonical_decimal_text(text: str) -> bool:
    return isinstance(text, str) and bool(_DECIMAL_TEXT_RE.match(text))


def decimal_fidelity_holds(value: Decimal, encode_fn) -> bool:
    """The CORE PROPERTY, parameterised over an encoder: encode(value) must
    parse back to the identical Decimal, exactly (not approximately).

    A broken encoder that produces text outside the canonical decimal shape
    entirely (e.g. "inf" from a float round trip that overflowed) is a
    fidelity VIOLATION, not a harness error -- so that case returns False
    rather than raising, keeping this usable uniformly against both the real
    encoder and the deliberately-broken stand-ins in this module.
    """
    text = encode_fn(value)
    if not is_canonical_decimal_text(text):
        return False
    return Decimal(text) == value
