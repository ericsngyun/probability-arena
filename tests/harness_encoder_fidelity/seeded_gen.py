"""Deterministic, SEEDED generation of semantic scalars and nested structures.

Hypothesis is NOT installed in this environment (verified: `import hypothesis`
raises ModuleNotFoundError with the repo's pinned venv). Per the task's
fallback instruction, this module hand-rolls property generation with
`random.Random(seed)` so every generated value is reproducible from an
integer seed alone -- printed in every failure message.

Nothing here is a test. It is pure generation, imported by the test file.
"""

from __future__ import annotations

import random
import string
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

UTC = timezone.utc

# A generous pool of "interesting" unicode code points: combining marks,
# right-to-left text, zero-width joiners, astral-plane emoji (surrogate pair
# in UTF-16 but a single scalar in Python str), and a handful of plain ASCII.
_UNICODE_POOL = [
    "a", "Z", "0", " ", "\t", "\n",
    "café", "naïve", "Москва", "北京", "😀", "🧑‍🚀",  # emoji + ZWJ sequence
    "​",           # zero-width space
    "́",           # combining acute accent, alone
    "é",          # e + combining acute (not the precomposed é)
    "‮" "reversed", # right-to-left override
    "￿",            # noncharacter, still valid UTF-8-encodable
    "",                  # empty string
]


class HostileUtcOffset(tzinfo):
    """A tzinfo whose utcoffset() raises. Legitimate producer-hostile input:
    nothing here is a subclass trick the encoder is expected to trust."""

    def utcoffset(self, dt):
        raise RuntimeError("hostile utcoffset")

    def dst(self, dt):
        return timedelta(0)

    def tzname(self, dt):
        return "HOSTILE"


@dataclass
class GenConfig:
    """Bounds for generation. Kept modest by default; the exhaustive corpus
    is opted into via KALSHI_HARNESS_EXHAUSTIVE=1 (see the test file)."""

    max_depth: int = 3
    max_container_size: int = 5
    decimal_digit_range: tuple = (1, 60)   # deliberately straddles the 28-digit
                                            # ambient decimal context boundary
    decimal_exponent_range: tuple = (-20, 20)
    int_bit_range: tuple = (0, 256)


def make_rng(seed: int) -> random.Random:
    return random.Random(seed)


def gen_decimal(rng: random.Random, cfg: GenConfig = GenConfig()) -> Decimal:
    lo, hi = cfg.decimal_digit_range
    ndigits = rng.randint(lo, hi)
    digits = "".join(rng.choice("0123456789") for _ in range(ndigits))
    # Avoid an all-zero significand collapsing to "0" and losing the digit
    # count the test wants to control.
    if digits.strip("0") == "":
        digits = "1" + digits[1:]
    sign = rng.choice(["", "-"])
    elo, ehi = cfg.decimal_exponent_range
    frac_len = rng.randint(0, max(0, min(ndigits, ehi - elo)))
    if frac_len and frac_len < len(digits):
        text = f"{sign}{digits[:-frac_len]}.{digits[-frac_len:]}"
    else:
        text = f"{sign}{digits}"
    return Decimal(text)


def gen_small_decimal(rng: random.Random) -> Decimal:
    """A decimal guaranteed to fit inside the ambient 28-digit precision --
    used for the TRUE property test (no known defect in this subrange)."""
    return gen_decimal(rng, GenConfig(decimal_digit_range=(1, 27),
                                      decimal_exponent_range=(-10, 10)))


def gen_large_decimal(rng: random.Random) -> Decimal:
    """A decimal beyond the ambient 28-digit precision but still inside the
    admitted 512-digit envelope -- this is where canonical_decimal() is known
    to corrupt the value silently."""
    return gen_decimal(rng, GenConfig(decimal_digit_range=(29, 512),
                                      decimal_exponent_range=(-50, 50)))


def gen_int(rng: random.Random, cfg: GenConfig = GenConfig()) -> int:
    lo, hi = cfg.int_bit_range
    bits = rng.randint(lo, hi)
    if bits == 0:
        return 0
    value = rng.getrandbits(bits) | (1 << (bits - 1))
    return -value if rng.random() < 0.5 else value


def gen_str(rng: random.Random) -> str:
    if rng.random() < 0.5:
        n = rng.randint(0, 6)
        return "".join(rng.choice(_UNICODE_POOL) for _ in range(n))
    n = rng.randint(0, 12)
    return "".join(rng.choice(string.printable) for _ in range(n))


def gen_lone_surrogate(rng: random.Random) -> str:
    """A lone UTF-16 surrogate: legal Python str, illegal UTF-8. Must be
    REFUSED by admission, not silently mutated -- this generator is used only
    by the refusal tests, never fed into the fidelity property directly."""
    cp = rng.randint(0xD800, 0xDFFF)
    return chr(cp)


def gen_datetime(rng: random.Random, *, allow_hostile: bool = False) -> datetime:
    choice = rng.random()
    if choice < 0.15:
        base = datetime(1, 1, 1, 0, 0, 0, 0, tzinfo=UTC)          # datetime.min
    elif choice < 0.3:
        base = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)  # datetime.max
    else:
        year = rng.randint(1970, 2100)
        base = datetime(year, rng.randint(1, 12), rng.randint(1, 28),
                        rng.randint(0, 23), rng.randint(0, 59),
                        rng.randint(0, 59), rng.randint(0, 999999),
                        tzinfo=UTC)
    if allow_hostile and rng.random() < 0.1:
        return base.replace(tzinfo=HostileUtcOffset())
    if rng.random() < 0.3:
        offset_hours = rng.choice([-12, -7, -4, 0, 3, 5.5, 9, 14])
        if isinstance(offset_hours, int):
            try:
                return base.astimezone(timezone(timedelta(hours=offset_hours)))
            except OverflowError:
                # base is at (or near) datetime.min/max, and this offset
                # would push the calendar out of range -- not the
                # generator's job to test that here (the encoder's own
                # overflow refusal is tested explicitly elsewhere); just
                # fall back to an in-range representation of the same
                # instant.
                return base
        return base
    return base


def gen_scalar(rng: random.Random, cfg: GenConfig = GenConfig()):
    """One admitted, non-container semantic value."""
    kind = rng.choice(["none", "bool", "int", "decimal", "str", "datetime"])
    if kind == "none":
        return None
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "int":
        return gen_int(rng, cfg)
    if kind == "decimal":
        return gen_decimal(rng, cfg)
    if kind == "str":
        return gen_str(rng)
    return gen_datetime(rng)


def gen_value(rng: random.Random, cfg: GenConfig = GenConfig(), depth: int = 0):
    """A scalar, or a nested list/mapping of them, bounded by cfg.max_depth."""
    if depth >= cfg.max_depth or rng.random() < 0.4:
        return gen_scalar(rng, cfg)
    kind = rng.choice(["list", "dict"])
    n = rng.randint(0, cfg.max_container_size)
    if kind == "list":
        return [gen_value(rng, cfg, depth + 1) for _ in range(n)]
    out = {}
    for _ in range(n):
        key = gen_str(rng) or "k"
        out[key] = gen_value(rng, cfg, depth + 1)
    return out


def seeds(n: int, base: int = 0):
    """n reproducible seeds. Every failure message should print its seed so
    the exact corpus member can be regenerated with make_rng(seed)."""
    return list(range(base, base + n))
