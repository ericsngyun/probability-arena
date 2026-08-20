"""Fee, priority-fee and tip separation (REALIZED-FILL-CORPUS-001 §3).

Three different lamport outflows that a cost model must never merge:

| term | who receives it | where it appears | moves with |
|---|---|---|---|
| base network fee | burned / leader split | inside `meta.fee` | signature count only |
| priority fee | leader | inside `meta.fee` | congestion, and OUR choice |
| tip | a block-engine tip account | **NOT in `meta.fee`** — an ordinary SOL transfer | congestion, and OUR choice |

Getting these confused corrupts the cost basis, and the failure is asymmetric:
a model that reads `meta.fee` and stops omits the tip entirely, and the tip is
the term that grows precisely in the congested blocks where a signal looks
strongest. `ALPHA-FACTORY-001` §5.3 makes an omitted bounded cost term a
`VOID_MEASUREMENT`, not a rounding error.

**Two independent derivations of the priority fee are computed and compared.**
Doctrine 4: a measurement that cannot tell you when it is meaningless is worse
than no measurement, and the cheapest way to know is a second estimator that
can disagree.

* **Residual** — `priority = meta.fee - base_fee`, `base_fee =
  LAMPORTS_PER_SIGNATURE x numRequiredSignatures`. Exact given the constant.
* **Budget** — `ceil(compute_unit_price x compute_unit_limit / 1e6)` read out
  of the ComputeBudget instructions.

If they disagree, the result is `CONFLICTING_SOURCES`, not whichever is
convenient.

**A deliberate correction to the common formulation.** The priority fee is
charged on the compute-unit **limit requested**, not on the units
**consumed**. A route that requests 1,400,000 CU and burns 180,000 pays for
1,400,000. Using `consumed` here understates the fee whenever the limit is
over-requested, which is the normal case for aggregator routes, and it
understates it in the same direction as every other optimistic error. The
residual derivation is immune to this, which is why it is the primary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    Observed,
    absent,
    observed,
)
from app.fills.b58 import b58decode

#: Mainnet lamports per signature. A protocol constant today, and an
#: ASSUMPTION, not a venue fact — it is falsifiable and §12 of the contract
#: says how. If it ever changes, the residual derivation silently
#: misattributes the difference to the priority fee; the budget cross-check is
#: what would catch it.
LAMPORTS_PER_SIGNATURE = 5_000

COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"

#: ComputeBudget instruction discriminators.
_IX_REQUEST_UNITS_DEPRECATED = 0
_IX_REQUEST_HEAP_FRAME = 1
_IX_SET_COMPUTE_UNIT_LIMIT = 2
_IX_SET_COMPUTE_UNIT_PRICE = 3

#: Runtime defaults when no SetComputeUnitLimit is present.
DEFAULT_CU_PER_INSTRUCTION = 200_000
MAX_CU_PER_TRANSACTION = 1_400_000

#: Block-engine tip accounts.
#:
#: PROVENANCE: these are the publicly documented Jito mainnet tip accounts.
#: They are **UNVERIFIED IN-REPO** until a fixture is captured that pays one,
#: at which point the fixture becomes the verification (doctrine 8: a name is
#: not evidence of semantics; observe what actually moves).
#: `tests/test_realized_fill_fixtures.py` asserts the verification status
#: rather than assuming it.
KNOWN_TIP_ACCOUNTS: frozenset[str] = frozenset(
    {
        "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
        "HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe",
        "Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY",
        "ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49",
        "DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh",
        "ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt",
        "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
        "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
    }
)


@dataclass(frozen=True, slots=True)
class LamportTransfer:
    source: str
    destination: str
    lamports: int


@dataclass(frozen=True, slots=True)
class ComputeBudgetSettings:
    unit_price_micro_lamports: Maybe[int]
    unit_limit: Maybe[int]
    #: True when the limit is the runtime default rather than an explicit
    #: SetComputeUnitLimit. The default depends on the instruction count, so
    #: the budget derivation is DERIVED_LOSSY in that case.
    limit_is_default: bool


def read_compute_budget(
    instructions: list[dict], *, instruction_count: int
) -> ComputeBudgetSettings:
    """Extract compute-unit price and limit from ComputeBudget instructions.

    Only TOP-LEVEL instructions are considered: ComputeBudget is a
    transaction-level directive and a CPI to it does nothing. Reading inner
    instructions here would let an unrelated inner call move our fee estimate.
    """
    price: Maybe[int] = absent(
        AbsenceReason.NOT_PROVIDED, "no SetComputeUnitPrice instruction"
    )
    limit: Maybe[int] = absent(
        AbsenceReason.NOT_PROVIDED, "no SetComputeUnitLimit instruction"
    )
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        if ix.get("programId") != COMPUTE_BUDGET_PROGRAM_ID:
            continue
        raw = ix.get("data")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            payload = b58decode(raw)
        except ValueError:
            continue
        if not payload:
            continue
        disc = payload[0]
        if disc == _IX_SET_COMPUTE_UNIT_PRICE and len(payload) >= 9:
            price = observed(
                int.from_bytes(payload[1:9], "little"),
                source="ComputeBudget.SetComputeUnitPrice",
            )
        elif disc == _IX_SET_COMPUTE_UNIT_LIMIT and len(payload) >= 5:
            limit = observed(
                int.from_bytes(payload[1:5], "little"),
                source="ComputeBudget.SetComputeUnitLimit",
            )
        elif disc == _IX_REQUEST_UNITS_DEPRECATED and len(payload) >= 5:
            limit = observed(
                int.from_bytes(payload[1:5], "little"),
                source="ComputeBudget.RequestUnits(deprecated)",
            )

    limit_is_default = False
    if isinstance(limit, Absent):
        limit_is_default = True
        limit = observed(
            min(
                DEFAULT_CU_PER_INSTRUCTION * max(instruction_count, 1),
                MAX_CU_PER_TRANSACTION,
            ),
            source="runtime default compute-unit limit",
        )
    return ComputeBudgetSettings(
        unit_price_micro_lamports=price,
        unit_limit=limit,
        limit_is_default=limit_is_default,
    )


def base_fee_lamports(num_required_signatures: Maybe[int]) -> Maybe[Decimal]:
    if isinstance(num_required_signatures, Absent):
        return absent(
            AbsenceReason.NOT_RECONSTRUCTABLE,
            "signature count unknown, so base fee cannot be separated",
        )
    return observed(
        Decimal(LAMPORTS_PER_SIGNATURE * num_required_signatures.value),
        source=f"{LAMPORTS_PER_SIGNATURE} lamports/signature x "
        f"{num_required_signatures.value}",
    )


def priority_fee_residual(
    total_fee: Maybe[Decimal], base_fee: Maybe[Decimal]
) -> Maybe[Decimal]:
    """`meta.fee - base_fee`. Primary derivation."""
    if isinstance(total_fee, Absent):
        return absent(total_fee.reason, "total fee absent")
    if isinstance(base_fee, Absent):
        return absent(base_fee.reason, "base fee absent")
    residual = total_fee.value - base_fee.value
    if residual < 0:
        # meta.fee below the signature floor falsifies LAMPORTS_PER_SIGNATURE.
        # Refusing here is the point: a negative priority fee would otherwise
        # be clamped to zero and read as "no priority fee paid".
        return absent(
            AbsenceReason.CONFLICTING_SOURCES,
            f"meta.fee {total_fee.value} is below the assumed base fee "
            f"{base_fee.value}; LAMPORTS_PER_SIGNATURE is falsified",
        )
    return observed(residual, source="meta.fee minus base fee")


def priority_fee_from_budget(budget: ComputeBudgetSettings) -> Maybe[Decimal]:
    """`ceil(price x limit / 1e6)`. Independent cross-check.

    Charged on the requested LIMIT, not on units consumed — see module
    docstring.
    """
    if isinstance(budget.unit_price_micro_lamports, Absent):
        # No price instruction means no priority fee was requested. That is an
        # observation, not a gap: the absence of the instruction IS the venue
        # fact.
        if budget.unit_price_micro_lamports.reason is AbsenceReason.NOT_PROVIDED:
            return observed(
                Decimal(0), source="no SetComputeUnitPrice instruction present"
            )
        return absent(budget.unit_price_micro_lamports.reason)
    if isinstance(budget.unit_limit, Absent):
        return absent(budget.unit_limit.reason, "compute-unit limit unknown")
    micro = budget.unit_price_micro_lamports.value * budget.unit_limit.value
    return observed(
        Decimal(math.ceil(micro / 1_000_000)),
        source="compute_unit_price x compute_unit_limit / 1e6"
        + (" (limit defaulted)" if budget.limit_is_default else ""),
    )


def reconcile_priority_fee(
    residual: Maybe[Decimal],
    budget_estimate: Maybe[Decimal],
    *,
    tolerance_lamports: int = 1,
) -> tuple[Maybe[Decimal], str | None]:
    """Return the priority fee and a note when the two derivations disagree.

    The residual wins when both are available and agree. When they disagree
    beyond `tolerance_lamports` the answer is `CONFLICTING_SOURCES` — the
    corpus reports the contradiction rather than picking the convenient side.
    """
    if isinstance(residual, Observed) and isinstance(budget_estimate, Observed):
        delta = abs(residual.value - budget_estimate.value)
        if delta <= tolerance_lamports:
            return residual, None
        return (
            absent(
                AbsenceReason.CONFLICTING_SOURCES,
                f"residual={residual.value} budget={budget_estimate.value} "
                f"delta={delta} lamports",
            ),
            f"priority-fee derivations disagree by {delta} lamports",
        )
    if isinstance(residual, Observed):
        return residual, "priority fee from residual only; no budget cross-check"
    if isinstance(budget_estimate, Observed):
        return (
            budget_estimate,
            "priority fee from compute budget only; no residual cross-check",
        )
    return residual, "priority fee not derivable by either route"


def find_lamport_transfers(instructions: list[dict]) -> list[LamportTransfer]:
    """All parsed System-program transfers, top-level and inner.

    Requires `encoding=jsonParsed`. Under raw encoding this returns [] and the
    caller must type the tip as `NOT_RECONSTRUCTABLE` rather than zero.
    """
    out: list[LamportTransfer] = []
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        if ix.get("program") != "system" and ix.get("programId") != SYSTEM_PROGRAM_ID:
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") not in ("transfer", "transferWithSeed"):
            continue
        info = parsed.get("info")
        if not isinstance(info, dict):
            continue
        src = info.get("source")
        dst = info.get("destination")
        lamports = info.get("lamports")
        if not isinstance(src, str) or not isinstance(dst, str):
            continue
        try:
            lamports_int = int(lamports)
        except (TypeError, ValueError):
            continue
        out.append(
            LamportTransfer(source=src, destination=dst, lamports=lamports_int)
        )
    return out


@dataclass(frozen=True, slots=True)
class TipFinding:
    tip_lamports: Maybe[Decimal]
    destinations: Maybe[tuple[str, ...]]
    #: lamports the party sent to accounts we could not classify. Surfaced so
    #: a reviewer can see a candidate tip to an account outside the registry
    #: instead of it silently becoming "no tip".
    unattributed_outflow_lamports: int
    notes: tuple[str, ...]


def attribute_tip(
    transfers: list[LamportTransfer],
    *,
    parsed_instructions_available: bool,
    party_accounts: frozenset[str],
    tip_accounts: frozenset[str] = KNOWN_TIP_ACCOUNTS,
) -> TipFinding:
    """Separate MEV tip from ordinary transfers.

    A tip is identified **by destination**, because that is the only thing that
    distinguishes it — on chain it is an ordinary `system::transfer`. The
    registry is therefore a closed list with stated provenance, and its
    incompleteness is reported, not hidden.
    """
    if not parsed_instructions_available:
        return TipFinding(
            tip_lamports=absent(
                AbsenceReason.NOT_RECONSTRUCTABLE,
                "instructions not available in parsed form; a tip is "
                "indistinguishable from any other lamport transfer",
            ),
            destinations=absent(AbsenceReason.NOT_RECONSTRUCTABLE),
            unattributed_outflow_lamports=0,
            notes=("tip not reconstructable without jsonParsed instructions",),
        )

    total = 0
    dests: list[str] = []
    unattributed = 0
    for t in transfers:
        if t.destination in tip_accounts:
            total += t.lamports
            dests.append(t.destination)
        elif t.source in party_accounts:
            unattributed += t.lamports

    notes: list[str] = []
    if unattributed:
        notes.append(
            f"{unattributed} lamports transferred to accounts outside the tip "
            "registry; if one is an unregistered tip account the tip is "
            "understated"
        )
    return TipFinding(
        tip_lamports=observed(
            Decimal(total),
            source="system transfers to registered tip accounts",
        ),
        destinations=observed(tuple(dests), source="transfer destinations"),
        unattributed_outflow_lamports=unattributed,
        notes=tuple(notes),
    )
