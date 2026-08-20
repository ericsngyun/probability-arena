"""Markout labeling at 1s / 5s / 30s / 5m (REALIZED-FILL-CORPUS-001 §4).

`VOLATILITY-STATE-ENGINE-001` §3 defines R4 *toxic flow* as *"post-fill markout
persistently adverse"* and marks it `NOT_COMPUTABLE:no_fill_history`. This
module is the half of that dependency that turns prices into labels. It does
not make R4 computable on its own — that still requires our own fills.

**The price source is part of the measurement, not a detail.**

A markout computed against a different venue is not the same quantity as one
computed against the pool we actually traded. The difference is a venue basis,
and a venue basis is *persistent and signed* — exactly the shape a toxicity
detector is looking for. Feed R4 cross-venue markouts and it will report
adverse selection that is really an arbitrage spread nobody was crossing. So
every `Markout` carries its `PriceSource`, no consumer may drop it, and
`worst_price_source` exists so an aggregate cannot silently inherit the
quality of its best member.

**Absence rules, and they are load-bearing (doctrine 10):**

* no observation anywhere near the horizon -> `NOT_PROVIDED`
* the horizon has not elapsed yet -> `NOT_YET_OBSERVED`
* observations exist but all fall outside tolerance -> `NOT_PROVIDED`, with
  the offset that disqualified them stated

A markout is **never** 0.0 because we lacked a price. `markout_5m = 0` means
the price 5 minutes later equalled the fill price, which is a strong and rare
claim about a memecoin pool.

**Interpolation is opt-in and typed.** `INTERPOLATED` is a different
measurement from `SAME_POOL_TRADE`: it assumes a path between two observations
that the pool did not necessarily take, and in a jump regime — the regime that
matters — that assumption is wrong in the direction that flatters us, because
interpolation removes exactly the excursion the markout is trying to catch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    Observed,
    absent,
    observed,
)
from app.fills.schema import (
    MARKOUT_HORIZONS_SECONDS,
    PRICE_SOURCE_QUALITY,
    Markout,
    PriceSource,
    Side,
)

#: An observation this far from the horizon, as a fraction of the horizon, is
#: not a measurement of that horizon. A 1 s markout taken 41 s late is a 41 s
#: markout wearing the wrong label, and mislabeling it is worse than losing it
#: because it survives into a statistic.
DEFAULT_TOLERANCE_FRACTION = Decimal("0.5")

#: Below this the tolerance stops shrinking. Chain and collector timestamps do
#: not resolve better than this, so demanding more is demanding noise.
#: LOUD PLACEHOLDER: 250 ms is asserted from the block cadence, NOT measured
#: on our own collector. The contract (§8) says what would falsify it.
MIN_TOLERANCE_MS = 250


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One price, with everything needed to judge whether it may be used.

    `price` MUST be quoted as **quote-asset units per unit of base asset** —
    the same orientation `fill_price_quote_per_base` produces. Mixing
    orientations inverts every markout sign and the result still looks like a
    number.
    """

    t: datetime
    price: Decimal
    source: PriceSource
    pool: Maybe[str]
    slot: Maybe[int]
    base_mint: str
    quote_mint: str


def fill_price_quote_per_base(
    side: Side, actual_price: Maybe[Decimal]
) -> Maybe[Decimal]:
    """Convert `actual_price` (output per input) to quote-per-base.

    This inversion is where a markout sign silently flips, so it lives in one
    named function with one test rather than inline at three call sites.

    * `DISPOSE` — base in, quote out, so output/input is already quote/base.
    * `ACQUIRE` — quote in, base out, so output/input is base/quote and must
      be inverted.
    """
    if isinstance(actual_price, Absent):
        return absent(actual_price.reason, "no realized price to orient")
    if actual_price.value == 0:
        return absent(
            AbsenceReason.NOT_RECONSTRUCTABLE, "realized price is zero"
        )
    if side is Side.DISPOSE:
        return observed(actual_price.value, source="actual_price (quote/base)")
    return observed(
        Decimal(1) / actual_price.value, source="1 / actual_price (inverted)"
    )


def _tolerance_ms(horizon_seconds: int, fraction: Decimal) -> int:
    return max(MIN_TOLERANCE_MS, int(Decimal(horizon_seconds * 1000) * fraction))


def label_markout(
    *,
    t_fill: datetime,
    horizon_seconds: int,
    observations: list[PriceObservation],
    now: datetime | None = None,
    tolerance_fraction: Decimal = DEFAULT_TOLERANCE_FRACTION,
    allow_interpolation: bool = False,
) -> Markout:
    """Label one horizon. Returns a `Markout` whose `price` may be `Absent`."""
    target = t_fill + timedelta(seconds=horizon_seconds)
    tolerance = _tolerance_ms(horizon_seconds, tolerance_fraction)

    if now is not None and now < target:
        return Markout(
            horizon_seconds=horizon_seconds,
            price=absent(
                AbsenceReason.NOT_YET_OBSERVED,
                f"horizon ends {target.isoformat()}, now {now.isoformat()}",
            ),
            source=PriceSource.NONE_AVAILABLE,
            observation_offset_ms=absent(AbsenceReason.NOT_YET_OBSERVED),
        )

    if not observations:
        return Markout(
            horizon_seconds=horizon_seconds,
            price=absent(
                AbsenceReason.NOT_PROVIDED,
                "no price observations supplied for this fill",
            ),
            source=PriceSource.NONE_AVAILABLE,
            observation_offset_ms=absent(AbsenceReason.NOT_PROVIDED),
        )

    def offset_ms(obs: PriceObservation) -> int:
        return int((obs.t - target).total_seconds() * 1000)

    nearest = min(observations, key=lambda o: abs(offset_ms(o)))
    delta = offset_ms(nearest)

    if abs(delta) <= tolerance:
        return Markout(
            horizon_seconds=horizon_seconds,
            price=observed(nearest.price, source=f"observation@{nearest.t.isoformat()}"),
            source=nearest.source,
            observation_offset_ms=observed(delta, source="observation.t - horizon"),
        )

    if allow_interpolation:
        before = [o for o in observations if o.t <= target]
        after = [o for o in observations if o.t >= target]
        if before and after:
            lo = max(before, key=lambda o: o.t)
            hi = min(after, key=lambda o: o.t)
            span = (hi.t - lo.t).total_seconds()
            if span > 0:
                weight = Decimal((target - lo.t).total_seconds()) / Decimal(span)
                price = lo.price + (hi.price - lo.price) * weight
                return Markout(
                    horizon_seconds=horizon_seconds,
                    price=observed(price, source="linear interpolation"),
                    # The source DOWNGRADES. An interpolated same-pool price is
                    # not a same-pool observation.
                    source=PriceSource.INTERPOLATED,
                    observation_offset_ms=observed(
                        0, source="interpolated exactly at the horizon"
                    ),
                )

    return Markout(
        horizon_seconds=horizon_seconds,
        price=absent(
            AbsenceReason.NOT_PROVIDED,
            f"nearest observation is {delta} ms from the horizon, outside the "
            f"{tolerance} ms tolerance; labelling it would relabel a different "
            "horizon as this one",
        ),
        source=PriceSource.NONE_AVAILABLE,
        observation_offset_ms=observed(delta, source="observation.t - horizon"),
    )


def label_markouts(
    *,
    t_fill: datetime,
    observations: list[PriceObservation],
    horizons: tuple[int, ...] = MARKOUT_HORIZONS_SECONDS,
    now: datetime | None = None,
    tolerance_fraction: Decimal = DEFAULT_TOLERANCE_FRACTION,
    allow_interpolation: bool = False,
) -> tuple[Markout, ...]:
    return tuple(
        label_markout(
            t_fill=t_fill,
            horizon_seconds=h,
            observations=observations,
            now=now,
            tolerance_fraction=tolerance_fraction,
            allow_interpolation=allow_interpolation,
        )
        for h in horizons
    )


def worst_price_source(markouts: tuple[Markout, ...]) -> PriceSource:
    """The quality an aggregate over these markouts actually has.

    Doctrine 10's inheritance rule: a feature inherits its source's data-
    quality capability. An average over one same-pool markout and three
    cross-venue ones is a cross-venue measurement.
    """
    usable = [m for m in markouts if isinstance(m.price, Observed)]
    if not usable:
        return PriceSource.NONE_AVAILABLE
    return max(usable, key=lambda m: PRICE_SOURCE_QUALITY[m.source]).source
