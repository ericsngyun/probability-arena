"""KALSHI-REALTIME-OBSERVATION-001A — exact fixed-point primitives.

Kalshi emits prices as decimal strings in dollars (`*_dollars`) and contract
counts as decimal strings (`*_fp`). The authorized contract is:

    price      0-4 decimal places, unit 0.0001 dollar   -> PRICE_SCALE    10_000
    contract   0-2 decimal places, unit 0.01 contract   -> CONTRACT_SCALE 100
    notional   price_units * contract_units             -> NOTIONAL_SCALE 1_000_000

Everything here is integer arithmetic over exactly-parsed decimals. **No value
in this module ever passes through `float`.** That is not stylistic: a book that
round-trips through binary floating point is wrong in the last place in a way
that survives every eyeball test and only shows up as an unexplained P&L
residual months later.

Parsing is deliberately strict and fails closed. A venue that starts emitting
exponent notation, five price decimals, or a NaN should break this parser loudly
on the first message rather than produce a plausible number.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation

PRICE_SCALE = 10_000          # 0.0001 dollar
CONTRACT_SCALE = 100          # 0.01 contract
NOTIONAL_SCALE = 1_000_000    # PRICE_SCALE * CONTRACT_SCALE

MAX_PRICE_DECIMALS = 4
MAX_CONTRACT_DECIMALS = 2

# One dollar in price units — the complement anchor.
ONE_DOLLAR_UNITS = 1 * PRICE_SCALE

# Bounds. A price outside [0, 1] dollar is not a probability; a count beyond
# this is far past any plausible venue depth and is treated as corruption.
MAX_PRICE_UNITS = ONE_DOLLAR_UNITS
MAX_CONTRACT_UNITS = 10**12

# Plain decimal only: optional sign, digits, optional single fractional part.
# Exponent notation is rejected on purpose — it is not documented for these
# fields, and silently accepting `1E-4` would make the decimal-place limits
# unenforceable.
_DECIMAL_RE = re.compile(r"^-?(?:\d+)(?:\.\d+)?$")


class FixedPointError(ValueError):
    """A value that must not enter the book."""


def _exact_decimal(raw, *, field: str) -> Decimal:
    """Parse to Decimal without ever touching float.

    Accepts `str` and `int`. A `float` input is refused outright rather than
    converted, because by the time a float exists the precision loss has already
    happened and converting it launders the error.
    """
    if isinstance(raw, bool):
        raise FixedPointError(f"{field}: bool is not a numeric value")
    if isinstance(raw, float):
        raise FixedPointError(
            f"{field}: received a float. Fixed-point values must be parsed from "
            "the venue's decimal string; a float has already lost precision and "
            "converting it would hide that")
    if isinstance(raw, int):
        return Decimal(raw)
    if not isinstance(raw, str):
        raise FixedPointError(f"{field}: expected a decimal string, got "
                              f"{type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise FixedPointError(f"{field}: empty value")
    low = text.lower()
    if low in ("nan", "-nan", "inf", "-inf", "infinity", "-infinity"):
        raise FixedPointError(f"{field}: {raw!r} is not a finite number")
    if not _DECIMAL_RE.match(text):
        raise FixedPointError(
            f"{field}: {raw!r} is not a plain decimal string (exponent "
            "notation, locale separators and signs-after-digits are rejected)")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - regex already guards
        raise FixedPointError(f"{field}: {raw!r} is not parseable") from exc
    if not value.is_finite():
        raise FixedPointError(f"{field}: {raw!r} is not finite")
    return value


def _decimal_places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def parse_price_units(raw, *, field: str = "price_dollars",
                      allow_negative: bool = False) -> int:
    """`"0.6150"` -> 6150 price units. Exact; never via float."""
    value = _exact_decimal(raw, field=field)
    places = _decimal_places(value)
    if places > MAX_PRICE_DECIMALS:
        raise FixedPointError(
            f"{field}: {raw!r} has {places} decimal places, more than the "
            f"{MAX_PRICE_DECIMALS} the price contract permits")
    if not allow_negative and value < 0:
        raise FixedPointError(f"{field}: {raw!r} is negative")
    scaled = value * PRICE_SCALE
    if scaled != scaled.to_integral_value():
        raise FixedPointError(f"{field}: {raw!r} is not representable in "
                              f"1/{PRICE_SCALE} dollar units")
    units = int(scaled)
    if abs(units) > MAX_PRICE_UNITS:
        raise FixedPointError(
            f"{field}: {raw!r} is outside [0, 1] dollar; a contract price is a "
            "probability and cannot exceed one dollar")
    return units


def parse_contract_units(raw, *, field: str = "contract_count_fp",
                         allow_negative: bool = False) -> int:
    """`"12.50"` -> 1250 contract units. Negative allowed only for deltas."""
    value = _exact_decimal(raw, field=field)
    places = _decimal_places(value)
    if places > MAX_CONTRACT_DECIMALS:
        raise FixedPointError(
            f"{field}: {raw!r} has {places} decimal places, more than the "
            f"{MAX_CONTRACT_DECIMALS} the contract-count contract permits")
    if not allow_negative and value < 0:
        raise FixedPointError(
            f"{field}: {raw!r} is negative; a resting quantity cannot be "
            "negative (a decrementing DELTA may be, and is parsed separately)")
    scaled = value * CONTRACT_SCALE
    if scaled != scaled.to_integral_value():
        raise FixedPointError(f"{field}: {raw!r} is not representable in "
                              f"1/{CONTRACT_SCALE} contract units")
    units = int(scaled)
    if abs(units) > MAX_CONTRACT_UNITS:
        raise FixedPointError(f"{field}: {raw!r} exceeds the configured bound")
    return units


def notional_units(price_units: int, contract_units: int) -> int:
    """Exact integer notional at NOTIONAL_SCALE (6 dollar decimals)."""
    return price_units * contract_units


def complement_price_units(price_units: int) -> int:
    """The YES price economically equivalent to a NO price at `price_units`.

    A resting NO bid at p is an offer of YES at (1 - p). Exact by construction:
    both sides live on the same integer grid, so the identity
    `yes + no == ONE_DOLLAR_UNITS` holds without rounding.
    """
    if not 0 <= price_units <= ONE_DOLLAR_UNITS:
        raise FixedPointError(
            f"price {price_units} is outside [0, {ONE_DOLLAR_UNITS}] and has no "
            "meaningful complement")
    return ONE_DOLLAR_UNITS - price_units


def format_price_units(price_units: int) -> str:
    """Back to a canonical decimal string. Round-trips exactly."""
    return str((Decimal(price_units) / PRICE_SCALE).quantize(
        Decimal(1).scaleb(-MAX_PRICE_DECIMALS)))


def format_contract_units(contract_units: int) -> str:
    return str((Decimal(contract_units) / CONTRACT_SCALE).quantize(
        Decimal(1).scaleb(-MAX_CONTRACT_DECIMALS)))


def loads_exact(text: str):
    """`json.loads` with every number parsed as Decimal.

    The venue sends numbers as strings for the fields that matter, but a stray
    bare number elsewhere in the payload must not become a float and leak into
    a derived value.
    """
    return json.loads(text, parse_float=Decimal, parse_int=int,
                      parse_constant=_reject_constant)


def _reject_constant(name: str):
    raise FixedPointError(f"JSON contained the non-finite constant {name!r}")


# --- price grid -----------------------------------------------------------------


class PriceGrid:
    """The set of prices a market actually admits.

    `price_ranges` from the venue is authoritative. The human-readable
    `price_level_structure` name is retained for reporting but is deliberately
    NOT used to derive numerical behaviour — a display label is not a contract,
    and keying arithmetic off one is how a market that quietly moves to a
    sub-cent grid starts silently rejecting valid prices.
    """

    # The venue's actual field names, confirmed against live demo REST on
    # 2026-08-07: `{"start": "0.0000", "end": "1.0000", "step": "0.0100"}`.
    # This class previously expected `start_dollars`/`end_dollars`/
    # `tick_dollars`, which are not names the venue sends — so every real
    # market raised a bare KeyError and the grid guard had never once run
    # against live data. The `_dollars` suffix does appear on scalar price
    # fields (`yes_bid_dollars`), which is presumably where the guess came
    # from; inside `price_ranges` it does not.
    _FIELD_ALIASES = {
        "start": ("start", "start_dollars"),
        "end": ("end", "end_dollars"),
        "step": ("step", "tick_dollars", "step_dollars"),
    }

    @classmethod
    def _field(cls, raw: dict, logical: str) -> str:
        for name in cls._FIELD_ALIASES[logical]:
            if name in raw:
                return raw[name]
        raise FixedPointError(
            f"price_ranges entry has no {logical!r} field; saw "
            f"{sorted(raw)} and accept {list(cls._FIELD_ALIASES[logical])}")

    def __init__(self, ranges, *, structure_name: str | None = None):
        self.structure_name = structure_name
        self.ranges: list[tuple[int, int, int]] = []
        for r in ranges or []:
            if not isinstance(r, dict):
                raise FixedPointError(
                    f"price_ranges entry is {type(r).__name__}, not an object")
            start = parse_price_units(self._field(r, "start"),
                                      field="price_ranges.start")
            end = parse_price_units(self._field(r, "end"), field="price_ranges.end")
            step = parse_price_units(self._field(r, "step"),
                                     field="price_ranges.step")
            if step <= 0:
                raise FixedPointError("price_ranges step must be positive")
            if end < start:
                raise FixedPointError("price_ranges end precedes start")
            self.ranges.append((start, end, step))
        self.ranges.sort()
        for (a_start, a_end, _), (b_start, _, _) in zip(self.ranges,
                                                        self.ranges[1:]):
            if b_start <= a_end:
                raise FixedPointError(
                    "price_ranges overlap; which step applies to a shared price "
                    "would be positional luck")

    @property
    def unconstrained(self) -> bool:
        return not self.ranges

    def contains(self, price_units: int) -> bool:
        if self.unconstrained:
            return 0 <= price_units <= ONE_DOLLAR_UNITS
        for start, end, step in self.ranges:
            if start <= price_units <= end and (price_units - start) % step == 0:
                return True
        return False

    def validate(self, price_units: int, *, field: str = "price") -> int:
        if not self.contains(price_units):
            raise FixedPointError(
                f"{field}: {format_price_units(price_units)} is off the market's "
                f"price grid ({self.describe()})")
        return price_units

    def describe(self) -> str:
        if self.unconstrained:
            return "unconstrained"
        return "; ".join(
            f"{format_price_units(s)}..{format_price_units(e)} step "
            f"{format_price_units(t)}" for s, e, t in self.ranges)
