"""Quote -> fill linkage (REALIZED-FILL-CORPUS-001 §4).

**Linkage is by carried identifier only. Timestamp proximity is refused.**

The temptation is obvious: we have a quote at `t_quote` and a confirmation at
`t_confirmed`, so match the nearest pair. That is a fit, not a link. Under
congestion — the only regime where `eps_fill` is large enough to matter —
quotes and submissions interleave, retries duplicate signatures, and
nearest-match systematically pairs each fill with whichever quote was closest
in time, which is biased toward the quote whose price the fill most resembles.
The residual then measures our matching rule instead of our execution.

So `link()` requires that the same `decision_id` was written on both records
**before** the fill existed, and it refuses otherwise. If a caller cannot
supply one, the honest output is `NOT_RECONSTRUCTABLE`, and a corpus with
fewer, correctly linked rows is worth more than a full one built on proximity.

The three refusals below each falsify the quote record rather than the fill,
which is why they are `CONFLICTING_SOURCES` and not a bad fill:

* **mint mismatch** — the quote and the fill are not about the same pair.
* **side mismatch** — the quote's direction and the fill's do not agree.
* **min-output breach** — `actual_output < quoted_min_output` on a *confirmed*
  transaction is impossible if the quote's floor was really enforced on chain;
  the route would have reverted. It means the recorded quote is not the quote
  that was executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    Observed,
    absent,
    observed,
)
from app.fills.schema import QuoteRecord, Side, TokenAmount
from app.seam.clock import ObservationTimestamp


@dataclass(frozen=True, slots=True)
class LinkResult:
    linked: bool
    method: str
    quote_to_submit_ms: Maybe[int]
    submit_to_confirm_ms: Maybe[int]
    realized_slippage: Maybe[Decimal]
    #: actual_input / quoted_input. < 1 is a partial fill.
    input_fill_ratio: Maybe[Decimal]
    is_partial: Maybe[bool]
    refusals: tuple[str, ...]
    notes: tuple[str, ...]


def _wall(stamp) -> datetime:
    """Wall-clock reading of either stamp type.

    `t_quote` / `t_submit` are `ObservationTimestamp` after
    SOCIAL-FILL-MEASUREMENT-SEAM-001; `t_confirmed` is a CHAIN-domain
    `datetime` and deliberately stays one (EVIDENCE-JOIN-CONTRACT-001 §3.3).
    """
    if isinstance(stamp, ObservationTimestamp):
        return stamp.wall_datetime
    return stamp


def _ms(a: Maybe, b: Maybe, what: str) -> Maybe[int]:
    """Millisecond difference between two fill-side stamps.

    **This is deliberately the UNANCHORED wall-clock difference.** These
    intervals are (i) submit->confirm, which crosses OURS -> CHAIN and can
    therefore never be monotonic, and (ii) quote->submit, whose stamps today
    come from producers that have not yet been migrated to
    `capture_observation`. Both are exposed to an NTP step and both say so on
    the record rather than pretending otherwise.

    The interval the contract actually cared about — social receipt -> quote —
    does NOT come through here. It goes through
    `app.seam.clock.our_response_latency`, which refuses an unanchored pair
    outright.
    """
    if isinstance(a, Absent):
        return absent(a.reason, f"{what}: start timestamp absent")
    if isinstance(b, Absent):
        return absent(b.reason, f"{what}: end timestamp absent")
    delta = _wall(b.value) - _wall(a.value)
    return observed(
        int(delta.total_seconds() * 1000),
        source=f"{what} (unanchored wall-clock difference)",
    )


def realized_slippage(
    quoted_price: Maybe[Decimal], actual_price: Maybe[Decimal]
) -> Maybe[Decimal]:
    """`(quoted_price - actual_price) / quoted_price`, both output-per-input.

    **Positive means we did WORSE than quoted** — we received less output per
    unit of input than the quote promised. The sign convention is stated once,
    here, and every consumer inherits it rather than re-deriving it.
    """
    if isinstance(quoted_price, Absent):
        return absent(quoted_price.reason, "no quoted price")
    if isinstance(actual_price, Absent):
        return absent(actual_price.reason, "no realized price")
    if quoted_price.value == 0:
        return absent(
            AbsenceReason.NOT_RECONSTRUCTABLE,
            "quoted price is zero; slippage undefined",
        )
    return observed(
        (quoted_price.value - actual_price.value) / quoted_price.value,
        source="(quoted_price - actual_price) / quoted_price",
    )


def link(
    *,
    quote: QuoteRecord,
    quote_decision_id: Maybe[str],
    fill_decision_id: Maybe[str],
    side: Side,
    t_submit: Maybe[datetime],
    t_confirmed: Maybe[datetime],
    actual_input: Maybe[TokenAmount],
    actual_output: Maybe[TokenAmount],
    actual_price: Maybe[Decimal],
) -> LinkResult:
    refusals: list[str] = []
    notes: list[str] = []

    if isinstance(quote_decision_id, Absent) or isinstance(fill_decision_id, Absent):
        return LinkResult(
            linked=False,
            method="none",
            quote_to_submit_ms=absent(
                AbsenceReason.NOT_RECONSTRUCTABLE, "no linkage"
            ),
            submit_to_confirm_ms=_ms(t_submit, t_confirmed, "submit->confirm"),
            realized_slippage=absent(
                AbsenceReason.NOT_RECONSTRUCTABLE, "no linkage"
            ),
            input_fill_ratio=absent(
                AbsenceReason.NOT_RECONSTRUCTABLE, "no linkage"
            ),
            is_partial=absent(AbsenceReason.NOT_RECONSTRUCTABLE, "no linkage"),
            refusals=(
                "no decision_id on one or both sides; timestamp-proximity "
                "matching is deliberately not implemented",
            ),
            notes=(),
        )

    if quote_decision_id.value != fill_decision_id.value:
        return LinkResult(
            linked=False,
            method="decision_id",
            quote_to_submit_ms=absent(
                AbsenceReason.CONFLICTING_SOURCES, "decision ids differ"
            ),
            submit_to_confirm_ms=_ms(t_submit, t_confirmed, "submit->confirm"),
            realized_slippage=absent(AbsenceReason.CONFLICTING_SOURCES),
            input_fill_ratio=absent(AbsenceReason.CONFLICTING_SOURCES),
            is_partial=absent(AbsenceReason.CONFLICTING_SOURCES),
            refusals=(
                f"decision_id mismatch: quote {quote_decision_id.value!r} vs "
                f"fill {fill_decision_id.value!r}",
            ),
            notes=(),
        )

    # --- the three falsifying checks -------------------------------------
    if isinstance(quote.quoted_input, Observed) and isinstance(actual_input, Observed):
        if quote.quoted_input.value.mint != actual_input.value.mint:
            refusals.append(
                f"input mint mismatch: quoted {quote.quoted_input.value.mint} "
                f"vs realized {actual_input.value.mint}"
            )
    if isinstance(quote.quoted_output, Observed) and isinstance(
        actual_output, Observed
    ):
        if quote.quoted_output.value.mint != actual_output.value.mint:
            refusals.append(
                f"output mint mismatch: quoted {quote.quoted_output.value.mint} "
                f"vs realized {actual_output.value.mint}"
            )
    if isinstance(quote.quoted_min_output, Observed) and isinstance(
        actual_output, Observed
    ):
        floor = quote.quoted_min_output.value
        if (
            floor.mint == actual_output.value.mint
            and actual_output.value.base_units < floor.base_units
        ):
            refusals.append(
                f"min-output breach: realized {actual_output.value.base_units} "
                f"< quoted floor {floor.base_units} on a confirmed "
                "transaction; the recorded quote is not the quote that "
                "executed"
            )

    if refusals:
        conflict = absent(AbsenceReason.CONFLICTING_SOURCES, "; ".join(refusals))
        return LinkResult(
            linked=False,
            method="decision_id",
            quote_to_submit_ms=_ms(quote.t_quote, t_submit, "quote->submit"),
            submit_to_confirm_ms=_ms(t_submit, t_confirmed, "submit->confirm"),
            realized_slippage=conflict,
            input_fill_ratio=conflict,
            is_partial=conflict,
            refusals=tuple(refusals),
            notes=tuple(notes),
        )

    # --- partial fill -----------------------------------------------------
    ratio: Maybe[Decimal] = absent(
        AbsenceReason.NOT_RECONSTRUCTABLE, "quoted or realized input absent"
    )
    partial: Maybe[bool] = absent(
        AbsenceReason.NOT_RECONSTRUCTABLE, "quoted or realized input absent"
    )
    if isinstance(quote.quoted_input, Observed) and isinstance(actual_input, Observed):
        quoted_units = quote.quoted_input.value.base_units
        if quoted_units == 0:
            ratio = absent(
                AbsenceReason.NOT_RECONSTRUCTABLE, "quoted input is zero"
            )
        else:
            value = Decimal(actual_input.value.base_units) / Decimal(quoted_units)
            ratio = observed(value, source="actual_input / quoted_input")
            partial = observed(value < 1, source="input_fill_ratio < 1")
            if value < 1:
                notes.append(
                    f"partial fill: {value:.6f} of the quoted input was "
                    "consumed; per-unit costs computed against the QUOTED "
                    "size would be understated"
                )
            elif value > 1:
                notes.append(
                    f"realized input EXCEEDS the quoted input (ratio {value}); "
                    "the fill is not the quoted trade"
                )

    return LinkResult(
        linked=True,
        method="decision_id",
        quote_to_submit_ms=_ms(quote.t_quote, t_submit, "quote->submit"),
        submit_to_confirm_ms=_ms(t_submit, t_confirmed, "submit->confirm"),
        realized_slippage=realized_slippage(quote.quoted_price, actual_price),
        input_fill_ratio=ratio,
        is_partial=partial,
        refusals=(),
        notes=tuple(notes),
    )
