"""REALIZED-FILL-CORPUS-001 — typed absence, markouts, linkage, eps_fill, AS_h.

Grouped in one module because they are one chain: absence must survive every
hop from the raw field to the calibration quantity, and testing the hops
separately is how a `None -> 0` sneaks into the last one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.fills.absence import (
    AbsenceReason,
    Absent,
    AbsenceError,
    Observed,
    absent,
    as_json,
    combine,
    observed,
    require,
    value_or,
)
from app.fills.calibration import (
    adverse_selection,
    adverse_selection_signed,
    all_in_cost,
    direction_sign,
    fill_residual,
    quoted_cost,
    realized_cost,
    relative_adverse_selection,
)
from app.fills.linkage import link, realized_slippage
from app.fills.markout import (
    PriceObservation,
    fill_price_quote_per_base,
    label_markout,
    label_markouts,
    worst_price_source,
)
from app.fills.schema import (
    CostBreakdown,
    Markout,
    PriceSource,
    QuoteRecord,
    Side,
    TokenAmount,
)

T0 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
SOL = "So11111111111111111111111111111111111111112"
TOKEN = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"


# ---------------------------------------------------------------------------
# typed absence
# ---------------------------------------------------------------------------


def test_absent_has_no_truth_value_so_x_or_zero_cannot_fabricate_a_zero():
    """`x or 0` is the single most common way an unknown becomes a zero. It is
    made impossible rather than discouraged."""
    a = absent(AbsenceReason.NOT_YET_OBSERVED, "5m has not elapsed")
    with pytest.raises(AbsenceError):
        bool(a)
    with pytest.raises(AbsenceError):
        a.unwrap()


def test_require_names_the_field_it_refused():
    a = absent(AbsenceReason.NOT_AUTHORIZED, "no calibration trades")
    with pytest.raises(AbsenceError) as exc:
        require(a, "eps_fill")
    assert "eps_fill" in str(exc.value)
    assert exc.value.reason is AbsenceReason.NOT_AUTHORIZED


def test_value_or_is_the_only_defaulting_path_and_it_is_greppable():
    assert value_or(observed(7, source="t"), 0) == 7
    assert value_or(absent(AbsenceReason.NOT_PROVIDED), 0) == 0


def test_observed_refuses_to_exist_without_a_provenance():
    with pytest.raises(ValueError):
        observed(Decimal(1), source="")


def test_a_sum_containing_an_unknown_term_is_unknown():
    """The most important line in the package. Total cost = fee + priority +
    tip, and quietly treating an unobserved tip as zero understates the cost
    basis by exactly the term that grows in a competitive block."""
    total = combine(
        observed(Decimal(5_000), source="t"),
        absent(AbsenceReason.NOT_RECONSTRUCTABLE, "unparsed instructions"),
        observed(Decimal(7_000), source="t"),
        source="test",
    )
    assert isinstance(total, Absent)
    assert total.reason is AbsenceReason.NOT_RECONSTRUCTABLE


def test_absence_survives_serialisation_as_a_reason_not_as_null():
    blob = as_json(absent(AbsenceReason.NOT_YET_OBSERVED, "horizon open"))
    assert blob == {
        "observed": False,
        "reason": "not_yet_observed",
        "detail": "horizon open",
    }
    assert "value" not in blob


def test_a_base_unit_count_without_decimals_is_not_a_quantity():
    amount = TokenAmount(
        mint=TOKEN,
        base_units=1_000_000,
        decimals=absent(AbsenceReason.NOT_PROVIDED, "decimals absent"),
    )
    scaled = amount.to_decimal()
    assert isinstance(scaled, Absent)


# ---------------------------------------------------------------------------
# markout labeling
# ---------------------------------------------------------------------------


def _obs(offset_seconds: float, price: str, source=PriceSource.SAME_POOL_TRADE):
    return PriceObservation(
        t=T0 + timedelta(seconds=offset_seconds),
        price=Decimal(price),
        source=source,
        pool=observed("pool1", source="t"),
        slot=observed(1, source="t"),
        base_mint=TOKEN,
        quote_mint=SOL,
    )


def test_an_unreached_horizon_is_not_yet_observed_never_zero():
    """POSITIVE CONTROL for the absence path: force the condition (ask for a
    5-minute markout 40 seconds after the fill) and require the label to
    become non-benign."""
    m = label_markout(
        t_fill=T0,
        horizon_seconds=300,
        observations=[_obs(1, "10")],
        now=T0 + timedelta(seconds=40),
    )
    assert isinstance(m.price, Absent)
    assert m.price.reason is AbsenceReason.NOT_YET_OBSERVED
    assert m.source is PriceSource.NONE_AVAILABLE


def test_no_observations_at_all_is_not_provided_never_zero():
    m = label_markout(t_fill=T0, horizon_seconds=5, observations=[])
    assert isinstance(m.price, Absent)
    assert m.price.reason is AbsenceReason.NOT_PROVIDED


def test_an_observation_outside_tolerance_is_refused_not_relabelled():
    """A 1s markout taken 41s late is a 41s markout wearing the wrong label.
    Mislabelling is worse than losing it, because it survives into a
    statistic."""
    m = label_markout(t_fill=T0, horizon_seconds=1, observations=[_obs(41, "10")])
    assert isinstance(m.price, Absent)
    assert m.price.reason is AbsenceReason.NOT_PROVIDED
    assert "outside the" in m.price.detail
    # the disqualifying offset is still reported
    assert isinstance(m.observation_offset_ms, Observed)
    assert m.observation_offset_ms.value == 40_000


def test_a_close_enough_observation_is_used_and_its_offset_recorded():
    m = label_markout(
        t_fill=T0, horizon_seconds=30, observations=[_obs(32, "11")]
    )
    assert isinstance(m.price, Observed)
    assert m.price.value == Decimal(11)
    assert m.observation_offset_ms.value == 2_000
    assert m.source is PriceSource.SAME_POOL_TRADE


def test_interpolation_is_opt_in_and_downgrades_the_source():
    """An interpolated same-pool price is NOT a same-pool observation. In a
    jump regime interpolation removes exactly the excursion the markout exists
    to catch, and it does so in the direction that flatters us."""
    obs = [_obs(-100, "10"), _obs(100, "20")]
    off = label_markout(t_fill=T0, horizon_seconds=5, observations=obs)
    assert isinstance(off.price, Absent)

    on = label_markout(
        t_fill=T0,
        horizon_seconds=5,
        observations=obs,
        allow_interpolation=True,
    )
    assert isinstance(on.price, Observed)
    assert on.source is PriceSource.INTERPOLATED
    assert on.source is not PriceSource.SAME_POOL_TRADE


def test_an_aggregate_inherits_the_worst_source_not_the_best():
    """Doctrine 10's inheritance rule. An average over one same-pool markout
    and one cross-venue markout is a cross-venue measurement."""
    good = label_markout(
        t_fill=T0,
        horizon_seconds=1,
        observations=[_obs(1, "10", PriceSource.SAME_POOL_TRADE)],
    )
    bad = label_markout(
        t_fill=T0,
        horizon_seconds=5,
        observations=[_obs(5, "10", PriceSource.OTHER_VENUE)],
    )
    assert worst_price_source((good, bad)) is PriceSource.OTHER_VENUE
    assert worst_price_source(()) is PriceSource.NONE_AVAILABLE


def test_all_four_horizons_are_labelled_and_none_is_silently_dropped():
    ms = label_markouts(t_fill=T0, observations=[_obs(1, "10")])
    assert tuple(m.horizon_seconds for m in ms) == (1, 5, 30, 300)


def test_the_orientation_inversion_is_explicit_and_side_dependent():
    """The single easiest place to invert every markout sign in the corpus."""
    price = observed(Decimal("0.0002"), source="t")  # output per input
    disposed = fill_price_quote_per_base(Side.DISPOSE, price)
    acquired = fill_price_quote_per_base(Side.ACQUIRE, price)
    assert disposed.value == Decimal("0.0002")
    assert acquired.value == Decimal(1) / Decimal("0.0002")


# ---------------------------------------------------------------------------
# AS_h
# ---------------------------------------------------------------------------


def test_adverse_selection_is_absent_when_the_markout_price_is_absent():
    m = Markout(
        horizon_seconds=300,
        price=absent(AbsenceReason.NOT_YET_OBSERVED, "5m open"),
        source=PriceSource.NONE_AVAILABLE,
        observation_offset_ms=absent(AbsenceReason.NOT_YET_OBSERVED),
    )
    result = adverse_selection(
        markout=m, fill_price_quote_per_base=observed(Decimal(10), source="t")
    )
    assert isinstance(result, Absent)
    assert result.reason is AbsenceReason.NOT_YET_OBSERVED


def test_as_h_is_the_literal_difference_and_carries_its_price_source():
    m = Markout(
        horizon_seconds=30,
        price=observed(Decimal("11"), source="t"),
        source=PriceSource.OTHER_VENUE,
        observation_offset_ms=observed(0, source="t"),
    )
    result = adverse_selection(
        markout=m, fill_price_quote_per_base=observed(Decimal("10"), source="t")
    )
    assert result.value == Decimal(1)
    # the source travels with the number; a consumer cannot lose it
    assert "other_venue" in result.source


def test_the_signed_markout_flips_with_the_side_and_negative_is_adverse():
    """R4 needs the SIGNED quantity. Fed the unsigned one, a toxicity detector
    would fire on direction instead of on toxicity."""
    m = Markout(
        horizon_seconds=30,
        price=observed(Decimal("11"), source="t"),
        source=PriceSource.SAME_POOL_TRADE,
        observation_offset_ms=observed(0, source="t"),
    )
    p_fill = observed(Decimal("10"), source="t")

    acquired = adverse_selection_signed(
        side=Side.ACQUIRE, markout=m, fill_price_quote_per_base=p_fill
    )
    disposed = adverse_selection_signed(
        side=Side.DISPOSE, markout=m, fill_price_quote_per_base=p_fill
    )
    # price rose after the fill: good if we acquired, adverse if we disposed
    assert acquired.value == Decimal(1)
    assert disposed.value == Decimal(-1)
    assert direction_sign(Side.ACQUIRE) == Decimal(1)
    assert direction_sign(Side.DISPOSE) == Decimal(-1)


def test_relative_adverse_selection_is_comparable_to_a_cost_floor():
    m = Markout(
        horizon_seconds=30,
        price=observed(Decimal("10.5"), source="t"),
        source=PriceSource.SAME_POOL_TRADE,
        observation_offset_ms=observed(0, source="t"),
    )
    p_fill = observed(Decimal("10"), source="t")
    signed = adverse_selection_signed(
        side=Side.ACQUIRE, markout=m, fill_price_quote_per_base=p_fill
    )
    rel = relative_adverse_selection(
        signed=signed, fill_price_quote_per_base=p_fill
    )
    assert rel.value == Decimal("0.05")


# ---------------------------------------------------------------------------
# eps_fill
# ---------------------------------------------------------------------------


def _costs(total_lamports: int) -> CostBreakdown:
    return CostBreakdown(
        network_fee_lamports=observed(Decimal(5_000), source="t"),
        priority_fee_lamports=observed(Decimal(total_lamports - 12_000), source="t"),
        tip_lamports=observed(Decimal(7_000), source="t"),
        tip_attempted_lamports=observed(Decimal(7_000), source="t"),
        compute_units_consumed=observed(1, source="t"),
        compute_unit_price_micro_lamports=observed(1, source="t"),
        rent_lamports_net=observed(Decimal(0), source="t"),
        tip_destinations=observed(("x",), source="t"),
    )


def test_eps_fill_against_the_quote_benchmark_is_slippage_plus_lamport_cost():
    """With `P_bench = quoted_price`, `C_quote_hat`'s price term is exactly
    zero and `eps_fill`'s price term is exactly the realized slippage. That is
    not a simplification — it is what "residual against the quote" means."""
    quoted_price = observed(Decimal("10"), source="quote")
    actual = observed(Decimal("11"), source="chain")  # paid more per base unit
    notional = observed(Decimal(10), source="t")  # 10 SOL

    realized = realized_cost(
        side=Side.ACQUIRE,
        actual_price=actual,
        price_bench=quoted_price,
        costs=_costs(1_012_000),
        notional_quote_units=notional,
        quote_asset_is_sol=True,
    )
    quoted = quoted_cost(
        side=Side.ACQUIRE,
        quoted_price=quoted_price,
        price_bench=quoted_price,
        modelled_lamport_costs=observed(Decimal(12_000), source="model"),
        notional_quote_units=notional,
        quote_asset_is_sol=True,
    )
    assert quoted.price_term.value == Decimal(0)

    eps = fill_residual(realized, quoted)
    assert isinstance(eps, Observed)
    # price term: (11-10)/10 = 0.1 ; lamport term: (1,012,000-12,000)/1e9/10
    assert eps.value == Decimal("0.1") + (Decimal(1_000_000) / Decimal(10**9) / 10)
    assert eps.value > 0  # the fill cost MORE than the quote predicted


def test_eps_fill_is_absent_when_either_side_is_absent_never_zero():
    """The corpus's core honesty property. A missing quote does not make the
    residual zero — it makes it unknown, and `NOT_AUTHORIZED` says waiting
    will never fix it."""
    realized = realized_cost(
        side=Side.ACQUIRE,
        actual_price=observed(Decimal("11"), source="chain"),
        price_bench=observed(Decimal("10"), source="t"),
        costs=_costs(12_000),
        notional_quote_units=observed(Decimal(10), source="t"),
        quote_asset_is_sol=True,
    )
    quoted = quoted_cost(
        side=Side.ACQUIRE,
        quoted_price=absent(
            AbsenceReason.NOT_AUTHORIZED, "no quote was ever requested"
        ),
        price_bench=observed(Decimal("10"), source="t"),
        modelled_lamport_costs=absent(AbsenceReason.NOT_AUTHORIZED),
        notional_quote_units=observed(Decimal(10), source="t"),
        quote_asset_is_sol=True,
    )
    eps = fill_residual(realized, quoted)
    assert isinstance(eps, Absent)
    assert eps.reason is AbsenceReason.NOT_AUTHORIZED


def test_a_non_sol_notional_refuses_the_lamport_term_rather_than_guessing():
    """Lamport costs are in SOL. Dividing them by a USDC notional without a
    SOL/USDC rate is exactly how a cost basis ends up wrong by the SOL price.
    The honest answer is a refusal."""
    terms = all_in_cost(
        side=Side.ACQUIRE,
        price_exec=observed(Decimal("11"), source="t"),
        price_bench=observed(Decimal("10"), source="t"),
        lamport_costs=observed(Decimal(12_000), source="t"),
        notional_quote_units=observed(Decimal(1_000), source="t"),
        quote_asset_is_sol=False,
        basis="test",
    )
    assert isinstance(terms.lamport_term, Absent)
    assert terms.lamport_term.reason is AbsenceReason.NOT_RECONSTRUCTABLE
    assert isinstance(terms.total, Absent)


def test_the_direction_sign_is_applied_once_and_in_one_place():
    acquire = all_in_cost(
        side=Side.ACQUIRE,
        price_exec=observed(Decimal("11"), source="t"),
        price_bench=observed(Decimal("10"), source="t"),
        lamport_costs=observed(Decimal(0), source="t"),
        notional_quote_units=observed(Decimal(1), source="t"),
        quote_asset_is_sol=True,
        basis="t",
    )
    dispose = all_in_cost(
        side=Side.DISPOSE,
        price_exec=observed(Decimal("11"), source="t"),
        price_bench=observed(Decimal("10"), source="t"),
        lamport_costs=observed(Decimal(0), source="t"),
        notional_quote_units=observed(Decimal(1), source="t"),
        quote_asset_is_sol=True,
        basis="t",
    )
    # paying more per base unit costs an acquirer and benefits a disposer
    assert acquire.price_term.value == Decimal("0.1")
    assert dispose.price_term.value == Decimal("-0.1")


# ---------------------------------------------------------------------------
# quote -> fill linkage
# ---------------------------------------------------------------------------


def _quote(
    *,
    price="10",
    input_units=1_000,
    output_units=10_000,
    min_output=9_000,
    capture_id="decision-1",
):
    return QuoteRecord(
        t_quote=observed(T0, source="t"),
        quoted_input=observed(
            TokenAmount(mint=SOL, base_units=input_units, decimals=observed(9, "t")),
            source="t",
        ),
        quoted_output=observed(
            TokenAmount(mint=TOKEN, base_units=output_units, decimals=observed(6, "t")),
            source="t",
        ),
        quoted_price=observed(Decimal(price), source="t"),
        quoted_price_impact=observed(Decimal("0.004"), source="t"),
        quoted_min_output=observed(
            TokenAmount(mint=TOKEN, base_units=min_output, decimals=observed(6, "t")),
            source="t",
        ),
        quote_source="t" and observed("test-quote-source", source="t"),
        quote_capture_id=observed(capture_id, source="t"),
    )


def _amount(mint, units, decimals=6):
    return observed(
        TokenAmount(mint=mint, base_units=units, decimals=observed(decimals, "t")),
        source="t",
    )


def test_linkage_requires_a_carried_identifier_and_refuses_proximity():
    """Timestamp-proximity matching is deliberately not implemented: it fits
    rather than links, and it is biased exactly under congestion."""
    result = link(
        quote=_quote(),
        quote_decision_id=absent(AbsenceReason.NOT_PROVIDED),
        fill_decision_id=absent(AbsenceReason.NOT_PROVIDED),
        side=Side.ACQUIRE,
        t_submit=observed(T0, source="t"),
        t_confirmed=observed(T0 + timedelta(seconds=2), source="t"),
        actual_input=_amount(SOL, 1_000, 9),
        actual_output=_amount(TOKEN, 9_500),
        actual_price=observed(Decimal("9.5"), source="t"),
    )
    assert result.linked is False
    assert "proximity" in result.refusals[0]
    # a latency we CAN measure is still reported
    assert result.submit_to_confirm_ms.value == 2_000


def test_a_mint_mismatch_falsifies_the_quote_not_the_fill():
    result = link(
        quote=_quote(),
        quote_decision_id=observed("decision-1", source="t"),
        fill_decision_id=observed("decision-1", source="t"),
        side=Side.ACQUIRE,
        t_submit=observed(T0, source="t"),
        t_confirmed=observed(T0 + timedelta(seconds=1), source="t"),
        actual_input=_amount(SOL, 1_000, 9),
        actual_output=_amount("SomeOtherMint1111111111111111111111111111111", 9_500),
        actual_price=observed(Decimal("9.5"), source="t"),
    )
    assert result.linked is False
    assert any("output mint mismatch" in r for r in result.refusals)
    assert isinstance(result.realized_slippage, Absent)
    assert result.realized_slippage.reason is AbsenceReason.CONFLICTING_SOURCES


def test_a_min_output_breach_on_a_confirmed_transaction_is_impossible():
    """If the quote's floor had really been enforced on chain, the route would
    have reverted. A confirmed fill below the floor means the recorded quote
    is not the quote that executed."""
    result = link(
        quote=_quote(min_output=9_900),
        quote_decision_id=observed("decision-1", source="t"),
        fill_decision_id=observed("decision-1", source="t"),
        side=Side.ACQUIRE,
        t_submit=observed(T0, source="t"),
        t_confirmed=observed(T0 + timedelta(seconds=1), source="t"),
        actual_input=_amount(SOL, 1_000, 9),
        actual_output=_amount(TOKEN, 9_500),
        actual_price=observed(Decimal("9.5"), source="t"),
    )
    assert result.linked is False
    assert any("min-output breach" in r for r in result.refusals)


def test_a_partial_fill_is_detected_and_flagged():
    result = link(
        quote=_quote(input_units=1_000),
        quote_decision_id=observed("decision-1", source="t"),
        fill_decision_id=observed("decision-1", source="t"),
        side=Side.ACQUIRE,
        t_submit=observed(T0 + timedelta(milliseconds=350), source="t"),
        t_confirmed=observed(T0 + timedelta(seconds=3), source="t"),
        actual_input=_amount(SOL, 400, 9),
        actual_output=_amount(TOKEN, 9_500),
        actual_price=observed(Decimal("9.5"), source="t"),
    )
    assert result.linked is True
    assert result.input_fill_ratio.value == Decimal("0.4")
    assert result.is_partial.value is True
    assert any("partial fill" in n for n in result.notes)
    assert result.quote_to_submit_ms.value == 350
    assert result.submit_to_confirm_ms.value == 2_650


def test_realized_slippage_sign_convention_positive_means_worse_than_quoted():
    worse = realized_slippage(
        observed(Decimal(10), source="t"), observed(Decimal(9), source="t")
    )
    better = realized_slippage(
        observed(Decimal(10), source="t"), observed(Decimal(11), source="t")
    )
    assert worse.value == Decimal("0.1")
    assert better.value == Decimal("-0.1")
