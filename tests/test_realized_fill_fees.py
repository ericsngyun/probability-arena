"""REALIZED-FILL-CORPUS-001 — fee, priority-fee and tip separation.

The headline control here is doctrine 8 applied to a formula rather than to a
field name: **the priority fee is charged on the compute-unit LIMIT REQUESTED,
not on the units CONSUMED.** That is measured on a real transaction below, not
assumed, and the two numbers differ by 60x on that transaction.
"""

from __future__ import annotations

import math
from decimal import Decimal
from pathlib import Path

import pytest

from app.fills.absence import AbsenceReason, Absent, Observed
from app.fills.b58 import b58decode, b58encode
from app.fills.decoder import decode_transaction
from app.fills.fees import (
    KNOWN_TIP_ACCOUNTS,
    LAMPORTS_PER_SIGNATURE,
    ComputeBudgetSettings,
    base_fee_lamports,
    priority_fee_from_budget,
    priority_fee_residual,
    read_compute_budget,
    reconcile_priority_fee,
)
from app.fills.provenance import load_fixture_set
from app.fills.absence import absent, observed

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "solana_fills"


@pytest.fixture(scope="module")
def fixtures():
    return load_fixture_set(FIXTURE_DIR)


# --- base58 ---------------------------------------------------------------


def test_b58_round_trips_including_leading_zero_bytes():
    for raw in (b"", b"\x00", b"\x00\x00\x01", b"\x03\x40\x77\x1b\x00\x00\x00\x00\x00"):
        assert b58decode(b58encode(raw)) == raw


def test_b58_rejects_a_non_alphabet_character():
    """Silently skipping an invalid character would shift every subsequent
    operand and produce a plausible wrong compute-unit price."""
    with pytest.raises(ValueError):
        b58decode("30I")  # 'I' is not in the bitcoin alphabet


def test_b58_decodes_a_real_compute_budget_operand(fixtures):
    """Verified against a real transaction whose priority fee is independently
    known from `meta.fee`."""
    payload = fixtures.payload(
        fixtures.by_capture_id("failed_transaction_v0_high_priority")
    )
    top = payload["transaction"]["message"]["instructions"]
    budget = read_compute_budget(top, instruction_count=len(top))
    assert budget.unit_price_micro_lamports.value == 3_333_333
    assert budget.unit_limit.value == 300_000
    assert budget.limit_is_default is False


# --- the formula ----------------------------------------------------------


def test_priority_fee_is_charged_on_the_requested_limit_not_units_consumed(
    fixtures,
):
    """MEASURED, on a real mainnet transaction.

    `4VFstbam…` consumed 4,919 compute units at a unit price of 3,333,333
    micro-lamports and paid a priority fee of exactly 1,000,000 lamports
    (`meta.fee` 1,005,000 minus a 5,000 base fee).

        price x CONSUMED        = ceil(3,333,333 x 4,919   / 1e6) =    16,397
        price x REQUESTED LIMIT = ceil(3,333,333 x 300,000 / 1e6) = 1,000,000

    The residual matches the LIMIT formulation exactly and the CONSUMED
    formulation is low by 61x (16,397 vs 1,000,000). A cost model using `consumed` would understate
    the priority fee on every over-requested route, which is most of them, and
    it would understate it in the same direction as every other optimistic
    error."""
    payload = fixtures.payload(
        fixtures.by_capture_id("failed_transaction_v0_high_priority")
    )
    d = decode_transaction(payload)

    residual = d.costs.priority_fee_lamports
    assert isinstance(residual, Observed)
    assert residual.value == Decimal(1_000_000)

    consumed = d.costs.compute_units_consumed.value
    price = d.costs.compute_unit_price_micro_lamports.value
    assert consumed == 4_919
    assert price == 3_333_333

    consumed_formula = math.ceil(price * consumed / 1_000_000)
    limit_formula = math.ceil(price * 300_000 / 1_000_000)
    assert consumed_formula == 16_397
    assert limit_formula == 1_000_000
    assert residual.value == Decimal(limit_formula)
    assert residual.value != Decimal(consumed_formula)


def test_the_two_derivations_agree_on_every_confirmed_fixture(fixtures):
    """Doctrine 4: the cross-check is what tells us the measurement is
    meaningful. If these ever disagree, the priority fee becomes
    CONFLICTING_SOURCES rather than a number."""
    checked = 0
    for entry in fixtures.entries:
        payload = fixtures.payload(entry)
        d = decode_transaction(payload, party=(entry.expected or {}).get("party"))
        assert isinstance(d.costs.priority_fee_lamports, Observed), entry.capture_id
        assert not any("disagree" in n for n in d.notes), entry.capture_id
        checked += 1
    assert checked == 6


def test_a_fee_below_the_signature_floor_falsifies_the_constant():
    """`LAMPORTS_PER_SIGNATURE` is an ASSUMPTION, not a venue fact. If
    `meta.fee` ever drops below it, the honest output is a refusal naming the
    falsified constant — not a priority fee clamped to zero, which would read
    as "no priority fee paid"."""
    total = observed(Decimal(3_000), source="test")
    base = base_fee_lamports(observed(1, source="test"))
    assert base.value == Decimal(LAMPORTS_PER_SIGNATURE)

    result = priority_fee_residual(total, base)
    assert isinstance(result, Absent)
    assert result.reason is AbsenceReason.CONFLICTING_SOURCES
    assert "falsified" in result.detail


def test_absent_signature_count_makes_the_base_fee_absent_not_zero():
    base = base_fee_lamports(absent(AbsenceReason.NOT_PROVIDED, "no header"))
    assert isinstance(base, Absent)
    assert base.reason is AbsenceReason.NOT_RECONSTRUCTABLE


def test_a_missing_price_instruction_is_an_observed_zero_not_an_absence():
    """The ABSENCE of a SetComputeUnitPrice instruction IS the venue fact that
    no priority fee was requested. This is the one place in the package where
    absence legitimately becomes a zero, so it is asserted explicitly rather
    than left to a reader to infer."""
    budget = ComputeBudgetSettings(
        unit_price_micro_lamports=absent(
            AbsenceReason.NOT_PROVIDED, "no SetComputeUnitPrice instruction"
        ),
        unit_limit=observed(200_000, source="default"),
        limit_is_default=True,
    )
    result = priority_fee_from_budget(budget)
    assert isinstance(result, Observed)
    assert result.value == Decimal(0)
    assert "no SetComputeUnitPrice" in result.source


def test_reconcile_refuses_to_pick_a_side_when_the_derivations_diverge():
    a = observed(Decimal(1_000_000), source="residual")
    b = observed(Decimal(16_398), source="budget")
    value, note = reconcile_priority_fee(a, b)
    assert isinstance(value, Absent)
    assert value.reason is AbsenceReason.CONFLICTING_SOURCES
    assert "disagree" in note


def test_reconcile_accepts_a_one_lamport_rounding_difference():
    a = observed(Decimal(1_000_000), source="residual")
    b = observed(Decimal(999_999), source="budget")
    value, note = reconcile_priority_fee(a, b)
    assert isinstance(value, Observed)
    assert value.value == Decimal(1_000_000)
    assert note is None


# --- the tip registry -----------------------------------------------------


def test_the_tip_account_registry_is_empirically_verified_not_assumed(fixtures):
    """Doctrine 8: a name is not evidence of its semantics, so the tip-account
    list must be observed doing what it claims.

    `KNOWN_TIP_ACCOUNTS` was written from public documentation, which is a
    claim about external reality. This test upgrades it to a measurement: at
    least one pinned real transaction must actually move lamports into an
    account on the list. If the list were wrong, every tip in the corpus would
    silently be zero and the cost basis would be understated by the single
    largest discretionary term."""
    verified: set[str] = set()
    for entry in fixtures.entries:
        payload = fixtures.payload(entry)
        keys = [
            k["pubkey"] if isinstance(k, dict) else k
            for k in payload["transaction"]["message"]["accountKeys"]
        ]
        pre = payload["meta"]["preBalances"]
        post = payload["meta"]["postBalances"]
        for i, key in enumerate(keys):
            if key in KNOWN_TIP_ACCOUNTS and post[i] - pre[i] > 0:
                verified.add(key)
    assert verified, (
        "no pinned fixture moves lamports into any registered tip account; "
        "the registry is unverified and every tip in the corpus would be a "
        "silent zero"
    )


def test_unregistered_outflow_is_reported_not_swallowed(fixtures):
    """A tip to an account outside the registry is indistinguishable from an
    ordinary transfer. The decoder cannot resolve that — but it must SAY so,
    so a reviewer sees the candidate instead of a confident zero."""
    payload = fixtures.payload(fixtures.by_capture_id("multi_hop_cyclic_route"))
    d = decode_transaction(payload)
    assert any("outside the tip registry" in n for n in d.notes)


def test_failed_transaction_unattributed_outflow_is_not_counted(fixtures):
    """On a failed transaction nothing moved, so an 'unattributed outflow'
    warning would be a false alarm about money that stayed put."""
    payload = fixtures.payload(
        fixtures.by_capture_id("failed_transaction_legacy_high_priority")
    )
    d = decode_transaction(payload)
    assert not any("outside the tip registry" in n for n in d.notes)
    assert any("reverted" in n for n in d.notes)


def test_total_cost_propagates_absence_rather_than_dropping_a_term(fixtures):
    """ALPHA-FACTORY-001 §5.3: any bounded cost term silently set to zero is a
    VOID_MEASUREMENT. A sum with an unknown term must be unknown."""
    from app.fills.schema import CostBreakdown

    costs = CostBreakdown(
        network_fee_lamports=observed(Decimal(5_000), source="t"),
        priority_fee_lamports=observed(Decimal(1_000), source="t"),
        tip_lamports=absent(AbsenceReason.NOT_RECONSTRUCTABLE, "unparsed"),
        tip_attempted_lamports=absent(AbsenceReason.NOT_RECONSTRUCTABLE),
        compute_units_consumed=absent(AbsenceReason.NOT_PROVIDED),
        compute_unit_price_micro_lamports=absent(AbsenceReason.NOT_PROVIDED),
        rent_lamports_net=observed(Decimal(0), source="t"),
        tip_destinations=absent(AbsenceReason.NOT_RECONSTRUCTABLE),
    )
    total = costs.total_lamports()
    assert isinstance(total, Absent)
    assert total.reason is AbsenceReason.NOT_RECONSTRUCTABLE
    assert "sum is unknown" in total.detail
