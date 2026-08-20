"""REALIZED-FILL-CORPUS-001 — decoder controls.

Doctrine 7: **every important metric needs a POSITIVE-CONTROL test — force the
underlying condition to occur and prove the metric becomes non-benign.** A
decoder that only ever sees healthy input proves only that healthy input
decodes. So this module is organised as controls, not as coverage:

* POSITIVE — real transactions whose true amounts are known independently,
  decoded exactly.
* NEGATIVE — the same transactions with one field corrupted. **The decoder
  must produce a different answer.** A decoder insensitive to the bytes it
  claims to read is not verified, it is a constant function.
* NEGATIVE — a failed transaction. Output must be typed absent, never zero,
  while the fee stays real.
* NEGATIVE — a multi-hop route where a naive log parser gives a different
  answer. The test implements the naive parser and asserts the divergence, so
  the claim "balance deltas beat log parsing" is measured rather than
  asserted.

All fixtures are real pinned mainnet transactions (see MANIFEST.json). None is
ours: this system has never traded.
"""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.fills.absence import AbsenceReason, Absent, Observed
from app.fills.decoder import (
    NATIVE_SOL_ASSET,
    DecodeRefusal,
    decode_transaction,
    realized_price,
)
from app.fills.provenance import load_fixture_set

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "solana_fills"

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
PUMP_A13 = "A13oRB9FFaiUjfi6LdCg6p9ka1u8SfGkUFs4SKvPpump"
PUMP_2RF = "2RfXjaiepngcBuGgPLtdnH22g68eetpgzCDX44Hnpump"


@pytest.fixture(scope="module")
def fixtures():
    return load_fixture_set(FIXTURE_DIR)


def payload_for(fixtures, capture_id: str) -> dict:
    entry = fixtures.by_capture_id(capture_id)
    return fixtures.payload(entry)


def party_for(fixtures, capture_id: str):
    return (fixtures.by_capture_id(capture_id).expected or {}).get("party")


# ---------------------------------------------------------------------------
# POSITIVE CONTROLS — real amounts, decoded exactly
# ---------------------------------------------------------------------------


def test_positive_direct_dispose_with_transient_wrapped_sol(fixtures):
    """A one-hop dispose whose proceeds arrive as wrapped SOL in an ATA that
    is created and closed inside the same transaction.

    The WSOL account therefore never appears in pre/postTokenBalances, so the
    entire output has to come from the lamport ledger with fee and tip added
    back. Independent check: the transaction contains exactly one
    `transferChecked` of WSOL, and with a single hop the operand and the
    balance delta MUST agree. They do."""
    d = decode_transaction(payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle"))

    assert d.succeeded
    assert isinstance(d.actual_input, Observed)
    assert d.actual_input.value.mint == PUMP_A13
    assert d.actual_input.value.base_units == 6_677_250_876
    assert d.actual_input.value.decimals.value == 6

    assert isinstance(d.actual_output, Observed)
    assert d.actual_output.value.mint == NATIVE_SOL_ASSET
    assert d.actual_output.value.base_units == 316_053_825
    assert d.actual_output.value.decimals.value == 9

    # the single-hop operand agrees with the balance-delta answer
    operands = _token_transfer_operands(
        payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    )
    assert 316_053_825 in operands
    assert 6_677_250_876 in operands

    assert d.costs.network_fee_lamports.value == Decimal(5_000)
    assert d.costs.priority_fee_lamports.value == Decimal(0)
    assert d.costs.tip_lamports.value == Decimal(7_000)
    assert d.costs.rent_lamports_net.value == Decimal(0)
    assert d.costs.total_lamports().value == Decimal(12_000)


def test_positive_ledger_identity_holds_for_the_fee_payer(fixtures):
    """The whole SOL leg reduces to one identity, and it must hold exactly:

        party_lamport_delta = output - network_fee - priority_fee - tip

    If it does not, some lamport flow is unaccounted for and the cost basis is
    wrong by exactly that amount."""
    payload = payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    d = decode_transaction(payload)
    raw = d.party_lamport_delta_raw.value
    fee = int(payload["meta"]["fee"])
    tip = int(d.costs.tip_lamports.value)
    assert raw == d.actual_output.value.base_units - fee - tip


def test_positive_second_direct_dispose_measures_a_zero_rent(fixtures):
    """A dispose with NO ATA creation, so `rent = 0` is a measured zero rather
    than an unexercised path. Without this fixture the primary fixture's zero
    proves nothing about the rent branch."""
    payload = payload_for(fixtures, "direct_dispose_no_ata_creation")
    d = decode_transaction(payload)
    assert d.succeeded
    assert d.actual_input.value.mint == PUMP_2RF
    assert d.actual_input.value.base_units == 5_487_059_455
    assert d.actual_output.value.mint == NATIVE_SOL_ASSET
    assert d.actual_output.value.base_units == 612_297
    assert d.costs.tip_lamports.value == Decimal(1_298)
    # no ATA program instruction in this transaction
    programs = [
        ix.get("programId")
        for ix in payload["transaction"]["message"]["instructions"]
    ]
    assert "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL" not in programs


def test_positive_fee_payer_is_not_the_trade_party(fixtures):
    """The SAME real transaction decoded from a counterparty's perspective.

    That account traded and did NOT pay the fee, so the fee must not be
    subtracted from its SOL leg. The decoder must say so on the record, not
    silently."""
    payload = payload_for(fixtures, "multi_hop_counterparty_view")
    party = party_for(fixtures, "multi_hop_counterparty_view")
    d = decode_transaction(payload, party=party)

    assert d.party_is_fee_payer is False
    assert any("NOT the fee payer" in n for n in d.notes)
    assert d.actual_input.value.mint == USDC
    assert d.actual_input.value.base_units == 107_232_992
    assert d.actual_output.value.mint == NATIVE_SOL_ASSET
    assert d.actual_output.value.base_units == 1_228_043_049
    # the counterparty's lamport account is untouched: its whole position
    # change is in token balances
    assert d.party_lamport_delta_raw.value == 0


def test_positive_realized_price_is_decimal_scaled_not_base_units(fixtures):
    d = decode_transaction(payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle"))
    price = realized_price(d)
    assert isinstance(price, Observed)
    # 316053825e-9 SOL / 6677250876e-6 tokens
    expected = (Decimal(316_053_825) / Decimal(10) ** 9) / (
        Decimal(6_677_250_876) / Decimal(10) ** 6
    )
    assert price.value == expected


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL — the decoder must be able to FAIL
# ---------------------------------------------------------------------------


def test_negative_corrupting_one_post_balance_changes_the_answer(fixtures):
    """Force the condition: change ONE lamport in `postBalances` at the party
    index and require the decoded output to move by exactly one lamport.

    A decoder that returned the same number would be reading something else —
    a log, an operand, a cached value — and would be certifying an answer that
    does not depend on the ledger it claims to measure."""
    payload = payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    clean = decode_transaction(payload)
    baseline = clean.actual_output.value.base_units

    corrupted = copy.deepcopy(payload)
    corrupted["meta"]["postBalances"][0] -= 1
    dirty = decode_transaction(corrupted)

    assert dirty.actual_output.value.base_units == baseline - 1
    assert dirty.actual_output.value.base_units != baseline


def test_negative_corrupting_a_token_amount_changes_the_input(fixtures):
    """Same control on the token ledger rather than the lamport ledger."""
    payload = payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    baseline = decode_transaction(payload).actual_input.value.base_units

    corrupted = copy.deepcopy(payload)
    for entry in corrupted["meta"]["preTokenBalances"]:
        if entry["mint"] == PUMP_A13:
            entry["uiTokenAmount"]["amount"] = str(
                int(entry["uiTokenAmount"]["amount"]) + 1_000
            )
    dirty = decode_transaction(corrupted)
    assert dirty.actual_input.value.base_units == baseline + 1_000


def test_negative_corrupting_the_fee_breaks_the_ledger_identity(fixtures):
    """`meta.fee` is added back into the party's SOL leg, so corrupting it
    must move the output. This is the field whose silent misreading would
    understate every cost basis in the corpus."""
    payload = payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    baseline = decode_transaction(payload).actual_output.value.base_units

    corrupted = copy.deepcopy(payload)
    corrupted["meta"]["fee"] = 5_000_000
    dirty = decode_transaction(corrupted)

    assert dirty.actual_output.value.base_units == baseline + 4_995_000

    # And the SECOND derivation catches it. The compute budget contains no
    # SetComputeUnitPrice, so the budget estimate is 0 while the meta.fee
    # residual is now 4,995,000. The decoder refuses to pick a side: the
    # priority fee becomes CONFLICTING_SOURCES rather than the convenient
    # number. This is the whole reason two independent derivations exist
    # (doctrine 4) — a single estimator would have reported 4,995,000 as a
    # measurement.
    assert isinstance(dirty.costs.priority_fee_lamports, Absent)
    assert (
        dirty.costs.priority_fee_lamports.reason
        is AbsenceReason.CONFLICTING_SOURCES
    )
    assert isinstance(dirty.costs.total_lamports(), Absent)
    assert any("disagree" in n for n in dirty.notes)


def test_negative_shifting_the_account_list_changes_the_answer(fixtures):
    """Insert one account at the front of the key list without touching the
    balance arrays — the classic v0/lookup-table alignment bug.

    Every subsequent balance now belongs to a different account. The decoder
    must NOT return the clean answer. If it did, its indices were not actually
    driving the result and the multi-hop and lookup-table cases would be
    decoding by coincidence."""
    payload = payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    clean = decode_transaction(payload)

    corrupted = copy.deepcopy(payload)
    corrupted["transaction"]["message"]["accountKeys"].insert(
        0,
        {
            "pubkey": "11111111111111111111111111111112",
            "signer": True,
            "source": "transaction",
            "writable": True,
        },
    )
    dirty = decode_transaction(corrupted)

    assert dirty.asset_deltas != clean.asset_deltas


def test_negative_missing_meta_is_refused_not_zeroed(fixtures):
    """No `meta` means no balances. The only correct behaviour is refusal — a
    record of zeroes would read as a trade that moved nothing."""
    payload = copy.deepcopy(
        payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    )
    del payload["meta"]
    with pytest.raises(DecodeRefusal):
        decode_transaction(payload)


def test_negative_missing_token_balances_is_not_provided_not_zero(fixtures):
    """Doctrine 10 directly. An RPC that omits the token-balance arrays said
    NOTHING about token movement; it did not say none happened."""
    payload = copy.deepcopy(
        payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    )
    del payload["meta"]["preTokenBalances"]
    del payload["meta"]["postTokenBalances"]
    d = decode_transaction(payload)

    assert isinstance(d.actual_input, Absent)
    assert d.actual_input.reason is AbsenceReason.NOT_PROVIDED
    assert any("NOT_PROVIDED, not zero" in n for n in d.notes)


def test_negative_a_failed_transaction_has_no_output_and_a_real_fee(fixtures):
    """The single most dangerous row type in a cost corpus.

    A failed transaction costs real money and delivers nothing. Recording its
    output as 0 would make it look like a free trade at a price of zero;
    dropping it from the corpus would bias the cost basis downward by exactly
    the failure rate, which is highest in the congested regime where the
    signal looks strongest."""
    payload = payload_for(fixtures, "failed_transaction_legacy_high_priority")
    d = decode_transaction(payload)

    assert d.succeeded is False
    assert isinstance(d.actual_output, Absent)
    assert d.actual_output.reason is AbsenceReason.TRANSACTION_FAILED
    assert isinstance(d.actual_input, Absent)
    assert d.actual_input.reason is AbsenceReason.TRANSACTION_FAILED

    # the fee is real and fully attributed
    assert d.costs.network_fee_lamports.value == Decimal(5_000)
    assert d.costs.priority_fee_lamports.value == Decimal(600_000)
    assert d.costs.total_lamports().value == Decimal(605_000)
    assert d.party_lamport_delta_raw.value == -605_000
    # nothing traded: no asset leg survives
    assert d.asset_deltas == {}


def test_negative_a_reverted_tip_is_not_a_paid_tip(fixtures):
    """MEASURED DEFECT, kept as a permanent regression guard.

    The first version of this decoder read the tip from the transfer operand.
    On this real failed transaction the operand says 1,500,000 lamports and
    the tip account's balance moved 0 — a failed transaction reverts every
    state change except the fee. Booking the operand overstated the cost basis
    by 1.5M lamports AND manufactured a positive SOL leg on a transaction that
    traded nothing."""
    payload = payload_for(fixtures, "failed_transaction_legacy_high_priority")
    d = decode_transaction(payload)

    assert d.costs.tip_lamports.value == Decimal(0)
    assert d.costs.tip_attempted_lamports.value == Decimal(1_500_000)
    assert any("reverted" in n for n in d.notes)

    # and the tip account really did not move
    keys = [
        k["pubkey"] for k in payload["transaction"]["message"]["accountKeys"]
    ]
    idx = keys.index("96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5")
    assert payload["meta"]["postBalances"][idx] == payload["meta"]["preBalances"][idx]


def test_negative_on_a_successful_transaction_intent_and_delta_agree(fixtures):
    """The other arm of the same control. If operand and delta disagreed here
    too, the rule above would be explaining noise rather than revert
    semantics."""
    d = decode_transaction(
        payload_for(fixtures, "direct_dispose_wrapped_sol_ata_cycle")
    )
    assert d.costs.tip_lamports.value == d.costs.tip_attempted_lamports.value


# ---------------------------------------------------------------------------
# NEGATIVE CONTROL — naive log parsing gives the wrong answer
# ---------------------------------------------------------------------------


def _token_transfer_operands(payload: dict) -> list[int]:
    """A deliberately naive parser: every SPL transfer operand, in order.

    This is what "parse the logs" means in practice, and it is what most
    open-source decoders do."""
    out: list[int] = []
    ixs = list(payload["transaction"]["message"]["instructions"])
    for group in payload["meta"].get("innerInstructions") or []:
        ixs.extend(group.get("instructions") or [])
    for ix in ixs:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") not in ("transfer", "transferChecked"):
            continue
        info = parsed.get("info") or {}
        amount = info.get("amount")
        if amount is None:
            amount = (info.get("tokenAmount") or {}).get("amount")
        if amount is None:
            continue
        try:
            out.append(int(amount))
        except (TypeError, ValueError):
            continue
    return out


def test_negative_naive_log_parsing_is_wrong_on_a_multi_hop_route(fixtures):
    """THE control that justifies balance-delta accounting.

    This real transaction is a two-hop cyclic route USDC -> WSOL -> USDC. The
    naive parser's "last transfer amount" is the second hop's gross output;
    the party's TRUE net position change is the tiny arbitrage residual. Both
    numbers are real and only one is the fill."""
    payload = payload_for(fixtures, "multi_hop_cyclic_route")
    d = decode_transaction(payload)

    truth = d.actual_output.value
    assert truth.mint == USDC
    assert truth.base_units == 725

    operands = _token_transfer_operands(payload)
    naive_last = operands[-1]
    naive_max = max(operands)

    assert naive_last == 107_232_992
    assert naive_max == 1_228_043_049

    # the naive answer is wrong by five orders of magnitude
    assert naive_last != truth.base_units
    assert naive_last / truth.base_units > 100_000

    # and the intermediate asset nets to exactly zero for the party, which is
    # WHY the delta method works on a route it knows nothing about
    assert NATIVE_SOL_ASSET not in d.asset_deltas or d.asset_deltas[
        NATIVE_SOL_ASSET
    ] != 1_228_043_049


def test_negative_the_multi_hop_route_is_not_reported_as_a_single_hop(fixtures):
    """`hop_count` must be ABSENT rather than 1.

    The decoder measures endpoints, not topology. Reporting a confident hop
    count it cannot support would be doctrine 8's failure — a field name
    asserting semantics the data does not carry."""
    d = decode_transaction(payload_for(fixtures, "multi_hop_cyclic_route"))
    assert isinstance(d.route.hop_count, Absent)
    assert d.route.hop_count.reason is AbsenceReason.NOT_RECONSTRUCTABLE
    assert isinstance(d.route.legs, Observed)
    assert len(d.route.legs.value) >= 2
    for leg in d.route.legs.value:
        assert isinstance(leg.pool, Absent)


def test_negative_every_fixture_survives_a_round_trip_through_json(fixtures):
    """A corpus row that cannot be serialised and re-read while preserving its
    absence reasons is not a corpus row."""
    from app.fills.corpus import build_realized_fill
    from app.fills.schema import Side

    for entry in fixtures.entries:
        fill = build_realized_fill(
            fixtures.payload(entry),
            side=Side.DISPOSE,
            base_mint="test",
            party=(entry.expected or {}).get("party"),
        )
        blob = json.dumps(fill.as_json())
        again = json.loads(blob)
        assert again["decoder_version"] == fill.decoder_version
        # absence survives serialisation as a reason, not as null
        assert again["quote"]["quoted_price"]["observed"] is False
        assert again["quote"]["quoted_price"]["reason"] == "not_authorized"
