"""The two quantities the corpus exists to produce (§5 of the contract).

    eps_fill = C_realized - C_quote_hat          the fill residual
    AS_h     = P_{t+h}   - P_fill                adverse selection at horizon h

Everything else in this package is machinery for computing these two honestly.

--------------------------------------------------------------------------
`eps_fill` — the fill residual
--------------------------------------------------------------------------

`AGENTS.md` says Solana is judged by `C_hat(s) - C_realized(s)`, and
`RISK-GOVERNOR-001` §10 calls the missing piece a *verified cost model*: "what
did the round trip actually cost, measured against realised fills". So cost
must be a single scalar in one unit, comparable ex-ante and ex-post.

**Definition.** All-in cost as a fraction of notional, against a declared
benchmark price `P_bench`, quoted in the same orientation for both terms:

    C(P_exec) = direction_sign * (P_exec - P_bench) / P_bench
              + (network_fee + priority_fee + tip) / notional_quote_units

* `direction_sign` is `+1` for `ACQUIRE` (paying more per base unit is a cost)
  and `-1` for `DISPOSE` (receiving less per base unit is a cost). The sign
  lives in one place because it is the single easiest thing to invert.
* `C_quote_hat` uses `P_exec = quoted_price` and the **modelled** lamport
  terms declared before submission.
* `C_realized` uses `P_exec = actual_price` and the **observed** lamport terms
  the decoder measured.

**`P_bench` must be identical for both terms or the subtraction is
meaningless.** The default is the quoted price itself, which is the ex-ante
benchmark by construction; with that default `C_quote_hat`'s price term is
exactly zero and `eps_fill`'s price term is exactly the realized slippage.
That is not a simplification, it is what "residual against the quote" means.

**A bounded cost term is never silently zero.** `ALPHA-FACTORY-001` §5.3 makes
any bounded cost term set to zero a `VOID_MEASUREMENT`, so an absent term
propagates absence through the sum instead of dropping out of it.

--------------------------------------------------------------------------
`AS_h` — adverse selection
--------------------------------------------------------------------------

    AS_h = P_{t+h} - P_fill

with both prices in **quote units per unit of base asset** (see
`markout.fill_price_quote_per_base`). Literally as specified, and unsigned by
direction — `AS_h` is a statement about the market, not about us.

`adverse_selection_signed` applies the direction and answers the question a
risk system actually asks: *did the price move against the position we took?*
Negative means adverse. Keeping the two apart matters, because R4 in
`VOLATILITY-STATE-ENGINE-001` is defined as *persistently adverse* markout,
and a detector fed the unsigned quantity would fire on direction rather than
on toxicity.

**Neither quantity may be computed from a markout whose price is `Absent`.**
There is no fallback, no zero, and no carrying-forward of the previous
horizon.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    absent,
    combine,
    observed,
)
from app.fills.schema import LAMPORTS_PER_SOL, CostBreakdown, Markout, Side


def direction_sign(side: Side) -> Decimal:
    return Decimal(1) if side is Side.ACQUIRE else Decimal(-1)


@dataclass(frozen=True, slots=True)
class CostTerms:
    """A cost decomposed so a reviewer can see which half moved."""

    price_term: Maybe[Decimal]
    lamport_term: Maybe[Decimal]
    total: Maybe[Decimal]
    basis: str


def all_in_cost(
    *,
    side: Side,
    price_exec: Maybe[Decimal],
    price_bench: Maybe[Decimal],
    lamport_costs: Maybe[Decimal],
    notional_quote_units: Maybe[Decimal],
    quote_asset_is_sol: bool,
    basis: str,
) -> CostTerms:
    """Cost as a fraction of notional. See the module docstring.

    `quote_asset_is_sol` is required, not inferred. Lamport costs are paid in
    SOL; expressing them as a fraction of a USDC notional needs a SOL/USDC
    rate we do not have here, so the honest answer is `NOT_RECONSTRUCTABLE`
    rather than a silently wrong denominator. This is the exact place where a
    cost basis would otherwise be off by the SOL price.
    """
    if isinstance(price_exec, Absent):
        price_term: Maybe[Decimal] = absent(
            price_exec.reason, "execution price absent"
        )
    elif isinstance(price_bench, Absent):
        price_term = absent(price_bench.reason, "benchmark price absent")
    elif price_bench.value == 0:
        price_term = absent(
            AbsenceReason.NOT_RECONSTRUCTABLE, "benchmark price is zero"
        )
    else:
        price_term = observed(
            direction_sign(side)
            * (price_exec.value - price_bench.value)
            / price_bench.value,
            source=f"{basis}: signed relative deviation from benchmark",
        )

    if not quote_asset_is_sol:
        lamport_term: Maybe[Decimal] = absent(
            AbsenceReason.NOT_RECONSTRUCTABLE,
            "lamport costs are denominated in SOL and the notional is not; a "
            "SOL/quote rate is required and is not supplied here",
        )
    elif isinstance(lamport_costs, Absent):
        lamport_term = absent(lamport_costs.reason, "lamport cost term absent")
    elif isinstance(notional_quote_units, Absent):
        lamport_term = absent(notional_quote_units.reason, "notional absent")
    elif notional_quote_units.value == 0:
        lamport_term = absent(AbsenceReason.NOT_RECONSTRUCTABLE, "zero notional")
    else:
        lamport_term = observed(
            (lamport_costs.value / Decimal(LAMPORTS_PER_SOL))
            / notional_quote_units.value,
            source=f"{basis}: lamport costs / notional",
        )

    return CostTerms(
        price_term=price_term,
        lamport_term=lamport_term,
        total=combine(price_term, lamport_term, source=f"{basis}: all-in cost"),
        basis=basis,
    )


def realized_cost(
    *,
    side: Side,
    actual_price: Maybe[Decimal],
    price_bench: Maybe[Decimal],
    costs: CostBreakdown,
    notional_quote_units: Maybe[Decimal],
    quote_asset_is_sol: bool,
) -> CostTerms:
    return all_in_cost(
        side=side,
        price_exec=actual_price,
        price_bench=price_bench,
        lamport_costs=costs.total_lamports(),
        notional_quote_units=notional_quote_units,
        quote_asset_is_sol=quote_asset_is_sol,
        basis="C_realized",
    )


def quoted_cost(
    *,
    side: Side,
    quoted_price: Maybe[Decimal],
    price_bench: Maybe[Decimal],
    modelled_lamport_costs: Maybe[Decimal],
    notional_quote_units: Maybe[Decimal],
    quote_asset_is_sol: bool,
) -> CostTerms:
    """`C_quote_hat`.

    `modelled_lamport_costs` must be the value declared BEFORE submission. A
    "model" fitted after the fills are known is not a prediction, and the
    residual against it is not a residual.
    """
    return all_in_cost(
        side=side,
        price_exec=quoted_price,
        price_bench=price_bench,
        lamport_costs=modelled_lamport_costs,
        notional_quote_units=notional_quote_units,
        quote_asset_is_sol=quote_asset_is_sol,
        basis="C_quote_hat",
    )


def fill_residual(realized: CostTerms, quoted: CostTerms) -> Maybe[Decimal]:
    """`eps_fill = C_realized - C_quote_hat`, as a fraction of notional.

    Positive means the fill cost MORE than the quote predicted, which is the
    direction that turns a real edge into a false graduate.
    """
    if isinstance(realized.total, Absent):
        return absent(realized.total.reason, "C_realized not computable")
    if isinstance(quoted.total, Absent):
        return absent(quoted.total.reason, "C_quote_hat not computable")
    return observed(
        realized.total.value - quoted.total.value,
        source="C_realized - C_quote_hat",
    )


def adverse_selection(
    *, markout: Markout, fill_price_quote_per_base: Maybe[Decimal]
) -> Maybe[Decimal]:
    """`AS_h = P_{t+h} - P_fill`. Unsigned by direction."""
    if isinstance(markout.price, Absent):
        return absent(
            markout.price.reason,
            f"no price at h={markout.horizon_seconds}s: {markout.price.detail}",
        )
    if isinstance(fill_price_quote_per_base, Absent):
        return absent(fill_price_quote_per_base.reason, "no fill price")
    return observed(
        markout.price.value - fill_price_quote_per_base.value,
        source=f"P_(t+{markout.horizon_seconds}s) - P_fill "
        f"[{markout.source.value}]",
    )


def adverse_selection_signed(
    *, side: Side, markout: Markout, fill_price_quote_per_base: Maybe[Decimal]
) -> Maybe[Decimal]:
    """Direction-applied markout. **Negative is adverse.**

    This is the quantity `VOLATILITY-STATE-ENGINE-001` R4 needs: persistently
    negative means the fills we get are the ones we did not want.
    """
    raw = adverse_selection(
        markout=markout, fill_price_quote_per_base=fill_price_quote_per_base
    )
    if isinstance(raw, Absent):
        return raw
    return observed(
        direction_sign(side) * raw.value,
        source=f"direction-applied {raw.source}",
    )


def relative_adverse_selection(
    *, signed: Maybe[Decimal], fill_price_quote_per_base: Maybe[Decimal]
) -> Maybe[Decimal]:
    """Signed markout as a fraction of the fill price, so it is comparable to
    `eps_fill` and to a cost floor in the same units."""
    if isinstance(signed, Absent):
        return signed
    if isinstance(fill_price_quote_per_base, Absent):
        return absent(fill_price_quote_per_base.reason, "no fill price")
    if fill_price_quote_per_base.value == 0:
        return absent(AbsenceReason.NOT_RECONSTRUCTABLE, "fill price is zero")
    return observed(
        signed.value / fill_price_quote_per_base.value,
        source="signed markout / P_fill",
    )
