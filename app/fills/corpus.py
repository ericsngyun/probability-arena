"""Corpus assembly: decoded transaction + quote + prices -> `RealizedFill`.

This is the seam. `RISK-GOVERNOR-001` §10 asks for one artifact, not five
modules, so something has to instantiate the real decoder, the real linkage
and the real markout labeller and produce the row a consumer reads. Doctrine 5
is explicit that a checkpoint is not complete until its production path is
demonstrably reachable **from outside**, which is why this function exists and
why `tests/test_realized_fill_seam.py` drives it end to end rather than
testing the parts.

Nothing here executes anything. It reads a confirmed transaction and writes a
record.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    Observed,
    absent,
)
from app.fills.decoder import (
    DECODER_VERSION,
    decode_transaction,
    realized_price,
)
from app.fills.linkage import link
from app.fills.markout import PriceObservation, label_markouts
from app.fills.schema import (
    MARKOUT_HORIZONS_SECONDS,
    FillStatus,
    QuoteRecord,
    RealizedFill,
    Side,
    StateLabels,
)


def empty_quote(reason: AbsenceReason, detail: str) -> QuoteRecord:
    """A quote record for a fill we did not quote.

    Every fixture in this corpus is a third-party transaction, so its quote
    side is genuinely absent — not zero, not reconstructed, not inferred from
    the outcome. `NOT_AUTHORIZED` is the correct reason for our own
    hypothetical fills: no quote exists because no trade was authorized, and
    waiting will not produce one.
    """
    a = absent(reason, detail)
    return QuoteRecord(
        t_quote=a,
        quoted_input=a,
        quoted_output=a,
        quoted_price=a,
        quoted_price_impact=a,
        quoted_min_output=a,
        quote_source=a,
        quote_capture_id=a,
    )


NO_STATES = StateLabels(
    liquidity_state=absent(
        AbsenceReason.NOT_PROVIDED, "no liquidity regime label supplied"
    ),
    volatility_state=absent(
        AbsenceReason.NOT_PROVIDED,
        "no volatility regime label supplied; note that R4 in "
        "VOLATILITY-STATE-ENGINE-001 is NOT_COMPUTABLE until this corpus has "
        "our own fills",
    ),
    social_state=absent(
        AbsenceReason.NOT_PROVIDED, "no social regime label supplied"
    ),
)


def build_realized_fill(
    payload: dict,
    *,
    side: Side,
    base_mint: str,
    party: str | None = None,
    quote: QuoteRecord | None = None,
    decision_id: Maybe[str] | None = None,
    observation_id: Maybe[str] | None = None,
    t_submit: Maybe[datetime] | None = None,
    price_observations: list[PriceObservation] | None = None,
    now: datetime | None = None,
    states: StateLabels = NO_STATES,
    model_version: Maybe[str] | None = None,
    notional_quote_units: Maybe[Decimal] | None = None,
    quote_asset_mint: Maybe[str] | None = None,
) -> RealizedFill:
    """Assemble one corpus row from a confirmed transaction.

    `side` and `base_mint` are supplied by the caller because they are facts
    about the DECISION, not about the transaction. The chain records that
    assets moved; it does not record which of them we considered the thing we
    were trading. Inferring the side from the deltas would work for most rows
    and be wrong for exactly the cyclic-route rows where it matters.
    """
    decoded = decode_transaction(payload, party=party)
    notes = list(decoded.notes)

    quote_record = quote or empty_quote(
        AbsenceReason.NOT_AUTHORIZED,
        "no quote exists for this transaction; this system has never "
        "requested one and is not authorized to (milestone §9)",
    )
    decision = decision_id or absent(
        AbsenceReason.NOT_APPLICABLE,
        "this transaction answers to no decision of ours",
    )
    submit_time = t_submit or absent(
        AbsenceReason.NOT_PROVIDED,
        "submission time is only observable by the submitter; for a "
        "third-party transaction it is NOT_RECONSTRUCTABLE from chain data",
    )

    price = realized_price(decoded)

    link_result = link(
        quote=quote_record,
        quote_decision_id=quote_record.quote_capture_id,
        fill_decision_id=decision,
        side=side,
        t_submit=submit_time,
        t_confirmed=decoded.block_time,
        actual_input=decoded.actual_input,
        actual_output=decoded.actual_output,
        actual_price=price,
    )
    notes.extend(link_result.notes)
    notes.extend(f"linkage refused: {r}" for r in link_result.refusals)

    if isinstance(decoded.block_time, Observed):
        markouts = label_markouts(
            t_fill=decoded.block_time.value,
            observations=price_observations or [],
            now=now,
        )
    else:
        from app.fills.schema import Markout, PriceSource

        markouts = tuple(
            Markout(
                horizon_seconds=h,
                price=absent(
                    AbsenceReason.NOT_RECONSTRUCTABLE,
                    "no confirmation time, so no horizon can be anchored",
                ),
                source=PriceSource.NONE_AVAILABLE,
                observation_offset_ms=absent(
                    AbsenceReason.NOT_RECONSTRUCTABLE
                ),
            )
            for h in MARKOUT_HORIZONS_SECONDS
        )

    if not decoded.succeeded:
        status = FillStatus.FAILED
    elif isinstance(decoded.actual_input, Absent) or isinstance(
        decoded.actual_output, Absent
    ):
        status = FillStatus.UNDECODABLE
    else:
        status = FillStatus.CONFIRMED

    return RealizedFill(
        decision_id=decision,
        observation_id=observation_id
        or absent(AbsenceReason.NOT_APPLICABLE, "no observation of ours"),
        mint=base_mint,
        side=side,
        notional_quote_units=notional_quote_units
        or absent(
            AbsenceReason.NOT_PROVIDED,
            "notional is a property of the decision, not of the transaction",
        ),
        quote_asset_mint=quote_asset_mint
        or absent(AbsenceReason.NOT_PROVIDED, "quote asset not declared"),
        route=decoded.route,
        quote=quote_record,
        t_submit=submit_time,
        signature=decoded.signature,
        slot=decoded.slot,
        t_confirmed=decoded.block_time,
        status=status,
        actual_input=decoded.actual_input,
        actual_output=decoded.actual_output,
        costs=decoded.costs,
        actual_price=price,
        realized_slippage=link_result.realized_slippage,
        quote_to_submit_ms=link_result.quote_to_submit_ms,
        submit_to_confirm_ms=link_result.submit_to_confirm_ms,
        markouts=markouts,
        states=states,
        model_version=model_version
        or absent(
            AbsenceReason.NOT_APPLICABLE,
            "no model produced this transaction; it is third-party evidence",
        ),
        decoder_version=DECODER_VERSION,
        reconstructability=dict(decoded.reconstructability),
        decoder_notes=tuple(notes),
    )


def corpus_summary(fills: list[RealizedFill]) -> dict:
    """Coverage of the corpus, reported as counts of PRESENT and ABSENT.

    Deliberately reports absence per field rather than averaging over the rows
    that happen to have a value. An average taken over present rows is a
    survivorship statistic, and `ALPHA-FACTORY-001` §5.3 treats a silently
    dropped cost term as a `VOID_MEASUREMENT` rather than a smaller sample.
    """
    total = len(fills)
    fields = {
        "actual_input": lambda f: f.actual_input,
        "actual_output": lambda f: f.actual_output,
        "actual_price": lambda f: f.actual_price,
        "network_fee": lambda f: f.costs.network_fee_lamports,
        "priority_fee": lambda f: f.costs.priority_fee_lamports,
        "tip": lambda f: f.costs.tip_lamports,
        "rent": lambda f: f.costs.rent_lamports_net,
        "realized_slippage": lambda f: f.realized_slippage,
        "quote_to_submit_ms": lambda f: f.quote_to_submit_ms,
        "submit_to_confirm_ms": lambda f: f.submit_to_confirm_ms,
    }
    coverage: dict[str, dict] = {}
    for name, get in fields.items():
        present = sum(1 for f in fills if isinstance(get(f), Observed))
        reasons: dict[str, int] = {}
        for f in fills:
            v = get(f)
            if isinstance(v, Absent):
                reasons[v.reason.value] = reasons.get(v.reason.value, 0) + 1
        coverage[name] = {
            "present": present,
            "absent": total - present,
            "absent_reasons": reasons,
        }
    markout_coverage = {}
    for h in MARKOUT_HORIZONS_SECONDS:
        present = 0
        reasons = {}
        for f in fills:
            m = f.markout(h)
            if m is None:
                continue
            if isinstance(m.price, Observed):
                present += 1
            else:
                reasons[m.price.reason.value] = (
                    reasons.get(m.price.reason.value, 0) + 1
                )
        markout_coverage[f"markout_{h}s"] = {
            "present": present,
            "absent": total - present,
            "absent_reasons": reasons,
        }
    return {
        "rows": total,
        "status": {
            s.value: sum(1 for f in fills if f.status is s) for s in FillStatus
        },
        "coverage": coverage,
        "markout_coverage": markout_coverage,
        "eps_fill_rows": 0,
        "eps_fill_note": (
            "eps_fill requires a quote recorded before submission and a fill "
            "of ours. Both require capital-funded calibration trades that are "
            "NOT authorized (milestone §9). This is NOT_AUTHORIZED, not "
            "not-yet-collected."
        ),
    }
