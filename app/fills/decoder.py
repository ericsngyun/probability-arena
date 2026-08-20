"""Solana confirmed-transaction decoder — balance-delta accounting.

**The rule: derive amounts from balance deltas, never from logs or instruction
operands.**

Logs and instruction operands describe *intent per hop*. A three-hop route
emits three "amount out" values and none of them is the trade's output; a
naive parser that takes the last one gets the output of the last hop, which is
right only when the last hop happens to end in the asset we wanted, and a
parser that takes the first gets the first hop's intermediate. Balance deltas
describe *what actually changed hands*, they are computed by the validator
rather than by a program, and they are already in `meta` — the intermediate
assets net to exactly zero for the trading party, so a multi-hop route decodes
with the same code as a direct one.

**Read-only.** This module consumes an RPC `getTransaction` result for a
transaction that has already confirmed. It constructs nothing, simulates
nothing, signs nothing and submits nothing.

Handled hard cases, and the ones that are refused rather than guessed, are
enumerated in `docs/milestones/REALIZED-FILL-CORPUS-001.md` §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from app.fills.absence import (
    AbsenceReason,
    Absent,
    Maybe,
    Observed,
    absent,
    observed,
)
from app.fills.fees import (
    KNOWN_TIP_ACCOUNTS,
    LamportTransfer,
    attribute_tip,
    base_fee_lamports,
    find_lamport_transfers,
    priority_fee_from_budget,
    priority_fee_residual,
    read_compute_budget,
    reconcile_priority_fee,
)
from app.fills.schema import (
    WRAPPED_SOL_MINT,
    CostBreakdown,
    Reconstructability,
    RouteDescriptor,
    RouteLeg,
    TokenAmount,
)

DECODER_VERSION = "realized-fill-decoder/1.0.0"

#: The pseudo-mint under which native SOL and wrapped SOL are reported as ONE
#: asset. They are the same economic asset and a route routinely moves value
#: between the two ledgers inside a single transaction; reporting them
#: separately is the fastest way to double-count an input.
NATIVE_SOL_ASSET = WRAPPED_SOL_MINT

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
MEMO_PROGRAM_IDS = frozenset(
    {
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",
        "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo",
    }
)
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"

#: Programs that are transaction plumbing rather than a trading venue. A
#: program NOT in this set that is invoked by the transaction is a *candidate*
#: venue leg — candidate, because this is a heuristic over program identity
#: and is typed DERIVED_LOSSY everywhere it is used.
INFRASTRUCTURE_PROGRAM_IDS = frozenset(
    {
        SYSTEM_PROGRAM_ID,
        TOKEN_PROGRAM_ID,
        TOKEN_2022_PROGRAM_ID,
        ATA_PROGRAM_ID,
        COMPUTE_BUDGET_PROGRAM_ID,
    }
    | MEMO_PROGRAM_IDS
)


class DecodeRefusal(Exception):
    """The decoder will not assert amounts for this transaction.

    Raised only for structural problems (missing `meta`, unusable account
    list). Economic ambiguity does NOT raise — it produces a decoded record
    whose amounts are `Absent` with a reason, because those records are
    evidence about the corpus and dropping them biases it toward the
    transactions we happen to understand.
    """


@dataclass(frozen=True, slots=True)
class AccountKey:
    index: int
    pubkey: str
    signer: bool
    writable: bool
    #: "transaction" for static keys, "lookupTable" for v0 loaded addresses.
    source: str


@dataclass(frozen=True, slots=True)
class TokenPosition:
    account_index: int
    account_pubkey: Maybe[str]
    mint: str
    owner: Maybe[str]
    pre_base_units: int
    post_base_units: int
    decimals: Maybe[int]
    #: False when the account had no token balance entry on that side. For a
    #: token account created inside the transaction, absence on the `pre` side
    #: genuinely means zero — the account did not exist, so it held nothing.
    #: This is one of the few places where absence and zero coincide, and it
    #: is asserted rather than assumed.
    pre_present: bool
    post_present: bool

    @property
    def delta(self) -> int:
        return self.post_base_units - self.pre_base_units


@dataclass(frozen=True, slots=True)
class DecodedTransaction:
    """Everything the decoder is willing to assert about one transaction."""

    signature: Maybe[str]
    slot: Maybe[int]
    block_time: Maybe[datetime]
    succeeded: bool
    err: Maybe[object]
    fee_payer: Maybe[str]
    party: str
    party_is_fee_payer: bool

    #: mint -> net base-unit change for the party, trade-only (fees, tips and
    #: rent removed). Native SOL and wrapped SOL are combined under
    #: `NATIVE_SOL_ASSET`.
    asset_deltas: dict[str, int]
    decimals_by_mint: dict[str, Maybe[int]]

    actual_input: Maybe[TokenAmount]
    actual_output: Maybe[TokenAmount]
    costs: CostBreakdown
    route: RouteDescriptor

    #: raw ledger components, kept for audit. A cost basis that cannot be
    #: re-derived from its inputs cannot be reviewed.
    party_lamport_delta_raw: Maybe[int]
    transfers: tuple[LamportTransfer, ...]
    token_positions: tuple[TokenPosition, ...]

    reconstructability: dict[str, Reconstructability] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    decoder_version: str = DECODER_VERSION


# --------------------------------------------------------------------------
# envelope parsing
# --------------------------------------------------------------------------


def _account_keys(payload: dict, meta: dict) -> list[AccountKey]:
    """Build the full account list in the order the balance arrays use.

    Two encodings must both work:

    * `jsonParsed` — `message.accountKeys` is a list of dicts and ALREADY
      includes lookup-table addresses, tagged `source: "lookupTable"`.
    * `json` — `message.accountKeys` is a list of strings containing only the
      static keys, and the loaded addresses must be appended in the order
      `loadedAddresses.writable` then `loadedAddresses.readonly`.

    Getting this order wrong silently shifts every balance by an account,
    which produces a completely wrong and completely plausible fill.
    """
    message = (payload.get("transaction") or {}).get("message") or {}
    raw_keys = message.get("accountKeys")
    if not isinstance(raw_keys, list) or not raw_keys:
        raise DecodeRefusal("transaction.message.accountKeys missing or empty")

    keys: list[AccountKey] = []
    if isinstance(raw_keys[0], dict):
        for i, entry in enumerate(raw_keys):
            keys.append(
                AccountKey(
                    index=i,
                    pubkey=str(entry.get("pubkey")),
                    signer=bool(entry.get("signer")),
                    writable=bool(entry.get("writable")),
                    source=str(entry.get("source") or "transaction"),
                )
            )
        return keys

    header = message.get("header") or {}
    num_sig = header.get("numRequiredSignatures")
    num_sig = num_sig if isinstance(num_sig, int) else 0
    for i, pubkey in enumerate(raw_keys):
        keys.append(
            AccountKey(
                index=i,
                pubkey=str(pubkey),
                signer=i < num_sig,
                writable=False,  # not reconstructable from `json` alone
                source="transaction",
            )
        )
    loaded = meta.get("loadedAddresses")
    if isinstance(loaded, dict):
        for pubkey in loaded.get("writable") or []:
            keys.append(
                AccountKey(
                    index=len(keys),
                    pubkey=str(pubkey),
                    signer=False,
                    writable=True,
                    source="lookupTable",
                )
            )
        for pubkey in loaded.get("readonly") or []:
            keys.append(
                AccountKey(
                    index=len(keys),
                    pubkey=str(pubkey),
                    signer=False,
                    writable=False,
                    source="lookupTable",
                )
            )
    return keys


def _all_instructions(payload: dict, meta: dict) -> tuple[list[dict], list[dict]]:
    """(top-level, top-level + inner) instruction dicts."""
    message = (payload.get("transaction") or {}).get("message") or {}
    top = [ix for ix in (message.get("instructions") or []) if isinstance(ix, dict)]
    every = list(top)
    for group in meta.get("innerInstructions") or []:
        if not isinstance(group, dict):
            continue
        for ix in group.get("instructions") or []:
            if isinstance(ix, dict):
                every.append(ix)
    return top, every


def _parsed_available(instructions: list[dict]) -> bool:
    return any("parsed" in ix or "program" in ix for ix in instructions)


def _token_positions(
    meta: dict, keys: list[AccountKey]
) -> tuple[list[TokenPosition], list[str]]:
    notes: list[str] = []
    pre_raw = meta.get("preTokenBalances")
    post_raw = meta.get("postTokenBalances")
    if pre_raw is None or post_raw is None:
        # doctrine 10: the RPC omitting the arrays is NOT "no token moved".
        notes.append(
            "preTokenBalances/postTokenBalances absent from meta; token "
            "movement is NOT_PROVIDED, not zero"
        )
        return [], notes

    def index_by(entries) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for entry in entries or []:
            if isinstance(entry, dict) and isinstance(entry.get("accountIndex"), int):
                out[entry["accountIndex"]] = entry
        return out

    pre = index_by(pre_raw)
    post = index_by(post_raw)
    positions: list[TokenPosition] = []
    for idx in sorted(set(pre) | set(post)):
        p = pre.get(idx)
        q = post.get(idx)
        ref = q or p or {}
        mint = ref.get("mint")
        if not isinstance(mint, str):
            notes.append(f"token balance at index {idx} has no mint; skipped")
            continue
        owner = ref.get("owner")
        owner_m: Maybe[str] = (
            observed(owner, source="meta token balance owner")
            if isinstance(owner, str)
            else absent(
                AbsenceReason.NOT_PROVIDED,
                "token balance entry carries no owner (pre-owner-field RPC)",
            )
        )

        def amount(entry) -> int:
            ui = (entry or {}).get("uiTokenAmount") or {}
            try:
                return int(ui.get("amount"))
            except (TypeError, ValueError):
                return 0

        dec_raw = ((q or p or {}).get("uiTokenAmount") or {}).get("decimals")
        decimals: Maybe[int] = (
            observed(int(dec_raw), source="meta uiTokenAmount.decimals")
            if isinstance(dec_raw, int)
            else absent(AbsenceReason.NOT_PROVIDED, "uiTokenAmount.decimals absent")
        )
        positions.append(
            TokenPosition(
                account_index=idx,
                account_pubkey=(
                    observed(keys[idx].pubkey, source="accountKeys")
                    if idx < len(keys)
                    else absent(
                        AbsenceReason.NOT_RECONSTRUCTABLE,
                        f"account index {idx} beyond accountKeys length "
                        f"{len(keys)} — the balance arrays and the account "
                        "list disagree",
                    )
                ),
                mint=mint,
                owner=owner_m,
                pre_base_units=amount(p),
                post_base_units=amount(q),
                decimals=decimals,
                pre_present=p is not None,
                post_present=q is not None,
            )
        )
    return positions, notes


def _candidate_route(top: list[dict], every: list[dict]) -> RouteDescriptor:
    """Candidate venue legs from program identity. DERIVED_LOSSY, always.

    This does not prove a route. It enumerates the non-infrastructure programs
    the transaction invoked, in order. Two hops through the same program
    collapse to two entries only if the program was invoked twice, and a
    program that routes internally shows as one. The measurement contract says
    plainly that `legs` is a candidate list; the *amounts* never depend on it.
    """
    legs: list[RouteLeg] = []
    for ix in every:
        program_id = ix.get("programId")
        if not isinstance(program_id, str):
            continue
        if program_id in INFRASTRUCTURE_PROGRAM_IDS:
            continue
        legs.append(
            RouteLeg(
                index=len(legs),
                program_id=observed(program_id, source="instruction programId"),
                pool=absent(
                    AbsenceReason.NOT_RECONSTRUCTABLE,
                    "pool identity requires per-program account-layout "
                    "knowledge this decoder deliberately does not encode",
                ),
                input_mint=absent(AbsenceReason.NOT_RECONSTRUCTABLE),
                output_mint=absent(AbsenceReason.NOT_RECONSTRUCTABLE),
            )
        )
    aggregator: Maybe[str] = absent(
        AbsenceReason.NOT_PROVIDED, "aggregator identity not supplied by caller"
    )
    if not legs:
        return RouteDescriptor(
            legs=observed((), source="no non-infrastructure program invoked"),
            hop_count=absent(
                AbsenceReason.NOT_RECONSTRUCTABLE,
                "no venue program identified; hop count unknown",
            ),
            aggregator=aggregator,
        )
    return RouteDescriptor(
        legs=observed(tuple(legs), source="candidate venue programs (DERIVED_LOSSY)"),
        hop_count=absent(
            AbsenceReason.NOT_RECONSTRUCTABLE,
            f"{len(legs)} venue-program invocations observed, which bounds but "
            "does not determine the hop count",
        ),
        aggregator=aggregator,
    )


# --------------------------------------------------------------------------
# the decoder
# --------------------------------------------------------------------------


def decode_transaction(
    payload: dict,
    *,
    party: str | None = None,
    tip_accounts: frozenset[str] = KNOWN_TIP_ACCOUNTS,
) -> DecodedTransaction:
    """Decode one `getTransaction` result into asserted amounts.

    `party` is the account whose economic position we are measuring. It
    defaults to the fee payer, which is the normal case and also the hard one:
    when the party *is* the fee payer, its lamport delta contains the fee, the
    priority fee, the tip and any rent, none of which are trade flow, and all
    of which must be added back before the SOL leg means anything.
    """
    if not isinstance(payload, dict):
        raise DecodeRefusal("payload is not an object")
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        # No meta means no balances. Refusing is mandatory: the alternative is
        # a record whose amounts are all zero and which looks like a trade
        # that moved nothing.
        raise DecodeRefusal("meta absent; balance deltas are unobtainable")

    notes: list[str] = []
    recon: dict[str, Reconstructability] = {}

    keys = _account_keys(payload, meta)
    by_pubkey = {k.pubkey: k for k in keys}
    top, every = _all_instructions(payload, meta)
    parsed_ok = _parsed_available(top) or _parsed_available(every)
    if not parsed_ok:
        notes.append(
            "instructions are not in parsed form; tip attribution and "
            "transfer-level detail are unavailable"
        )

    signatures = (payload.get("transaction") or {}).get("signatures") or []
    signature: Maybe[str] = (
        observed(str(signatures[0]), source="transaction.signatures[0]")
        if signatures
        else absent(AbsenceReason.NOT_PROVIDED, "no signature in payload")
    )
    recon["signature"] = Reconstructability.VENUE_FACT

    slot_raw = payload.get("slot")
    slot: Maybe[int] = (
        observed(int(slot_raw), source="getTransaction.slot")
        if isinstance(slot_raw, int)
        else absent(AbsenceReason.NOT_PROVIDED, "slot absent")
    )
    recon["slot"] = Reconstructability.VENUE_FACT

    bt_raw = payload.get("blockTime")
    block_time: Maybe[datetime] = (
        observed(
            datetime.fromtimestamp(int(bt_raw), tz=timezone.utc),
            source="getTransaction.blockTime",
        )
        if isinstance(bt_raw, int)
        else absent(
            AbsenceReason.NOT_PROVIDED,
            "blockTime absent — the ledger does not always carry one, and it "
            "is an estimate produced by validator vote timestamps even when "
            "present",
        )
    )
    recon["t_confirmed"] = Reconstructability.VENUE_FACT

    err = meta.get("err")
    succeeded = err is None
    err_m: Maybe[object] = (
        absent(AbsenceReason.NOT_APPLICABLE, "transaction succeeded")
        if succeeded
        else observed(err, source="meta.err")
    )
    if not succeeded:
        notes.append(
            f"transaction FAILED on chain ({err!r}); fees were still charged "
            "and state changes reverted"
        )

    fee_payer: Maybe[str] = (
        observed(keys[0].pubkey, source="accountKeys[0]")
        if keys
        else absent(AbsenceReason.NOT_PROVIDED)
    )
    resolved_party = party or (keys[0].pubkey if keys else None)
    if resolved_party is None:
        raise DecodeRefusal("cannot resolve a trading party")
    party_is_fee_payer = isinstance(fee_payer, Observed) and (
        fee_payer.value == resolved_party
    )
    if party is None:
        notes.append(
            "party defaulted to the fee payer; if a relayer paid, the fee "
            "attributed here is not the party's cost"
        )
    if not party_is_fee_payer:
        notes.append(
            "party is NOT the fee payer; network/priority fees are charged to "
            "another account and are reported but not deducted from the "
            "party's SOL leg"
        )

    # --- fees ------------------------------------------------------------
    fee_raw = meta.get("fee")
    total_fee: Maybe[Decimal] = (
        observed(Decimal(int(fee_raw)), source="meta.fee")
        if isinstance(fee_raw, int)
        else absent(AbsenceReason.NOT_PROVIDED, "meta.fee absent")
    )
    header = ((payload.get("transaction") or {}).get("message") or {}).get(
        "header"
    ) or {}
    num_sig_raw = header.get("numRequiredSignatures")
    num_sig: Maybe[int] = (
        observed(int(num_sig_raw), source="message.header.numRequiredSignatures")
        if isinstance(num_sig_raw, int)
        else (
            observed(len(signatures), source="len(transaction.signatures)")
            if signatures
            else absent(AbsenceReason.NOT_PROVIDED, "signature count unknown")
        )
    )
    base_fee = base_fee_lamports(num_sig)
    budget = read_compute_budget(top, instruction_count=len(top))
    prio_residual = priority_fee_residual(total_fee, base_fee)
    prio_budget = priority_fee_from_budget(budget)
    priority_fee, prio_note = reconcile_priority_fee(prio_residual, prio_budget)
    if prio_note:
        notes.append(prio_note)
    recon["network_fee"] = Reconstructability.VENUE_FACT
    recon["priority_fee"] = (
        Reconstructability.DERIVED
        if isinstance(prio_residual, Observed)
        else Reconstructability.DERIVED_LOSSY
    )

    cu_consumed_raw = meta.get("computeUnitsConsumed")
    cu_consumed: Maybe[int] = (
        observed(int(cu_consumed_raw), source="meta.computeUnitsConsumed")
        if isinstance(cu_consumed_raw, int)
        else absent(
            AbsenceReason.NOT_PROVIDED,
            "meta.computeUnitsConsumed absent (pre-1.15 ledger)",
        )
    )

    # --- token positions --------------------------------------------------
    positions, pos_notes = _token_positions(meta, keys)
    notes.extend(pos_notes)
    token_balances_provided = not any(
        "preTokenBalances/postTokenBalances absent" in n for n in pos_notes
    )

    party_token_accounts: list[TokenPosition] = []
    unowned = 0
    for pos in positions:
        if isinstance(pos.owner, Observed):
            if pos.owner.value == resolved_party:
                party_token_accounts.append(pos)
        else:
            unowned += 1
    if unowned:
        notes.append(
            f"{unowned} token balance entries carry no owner; they cannot be "
            "attributed to the party and any flow through them is invisible"
        )

    party_account_pubkeys = {resolved_party} | {
        p.account_pubkey.value
        for p in party_token_accounts
        if isinstance(p.account_pubkey, Observed)
    }

    # --- tip --------------------------------------------------------------
    transfers = find_lamport_transfers(every) if parsed_ok else []
    tip = attribute_tip(
        transfers,
        parsed_instructions_available=parsed_ok,
        party_accounts=frozenset(party_account_pubkeys),
        tip_accounts=tip_accounts,
    )
    notes.extend(tip.notes)
    recon["tip"] = (
        Reconstructability.DERIVED
        if isinstance(tip.tip_lamports, Observed)
        else Reconstructability.NOT_RECONSTRUCTABLE
    )

    # --- lamport ledger ---------------------------------------------------
    pre_bal = meta.get("preBalances")
    post_bal = meta.get("postBalances")
    balances_ok = (
        isinstance(pre_bal, list)
        and isinstance(post_bal, list)
        and len(pre_bal) == len(post_bal) == len(keys)
    )
    if not balances_ok:
        notes.append(
            "preBalances/postBalances missing or not aligned with the account "
            "list; the SOL leg is NOT_RECONSTRUCTABLE"
        )

    party_key = by_pubkey.get(resolved_party)
    party_lamport_delta_raw: Maybe[int]
    if not balances_ok:
        party_lamport_delta_raw = absent(
            AbsenceReason.NOT_RECONSTRUCTABLE, "balance arrays unusable"
        )
    elif party_key is None:
        # An account the transaction never referenced cannot have changed
        # balance. This is a genuine zero and it is asserted, not assumed.
        party_lamport_delta_raw = observed(
            0, source="party absent from accountKeys, so its balance is unchanged"
        )
    else:
        party_lamport_delta_raw = observed(
            int(post_bal[party_key.index]) - int(pre_bal[party_key.index]),
            source="postBalances - preBalances at the party index",
        )

    #: Rent held in the party's own token accounts, EXCLUDING any wrapped-SOL
    #: balance sitting inside them. A wrapped-SOL account's lamports are
    #: `rent + wrapped amount`; counting the whole lamport delta as rent and
    #: the token delta as flow double-counts the wrap.
    rent_net: Maybe[Decimal]
    if not balances_ok:
        rent_net = absent(AbsenceReason.NOT_RECONSTRUCTABLE, "balance arrays unusable")
    else:
        total_rent = 0
        for pos in party_token_accounts:
            if pos.account_index >= len(pre_bal):
                continue
            lamport_delta = int(post_bal[pos.account_index]) - int(
                pre_bal[pos.account_index]
            )
            if pos.mint == WRAPPED_SOL_MINT:
                lamport_delta -= pos.delta
            total_rent += lamport_delta
        rent_net = observed(
            Decimal(total_rent),
            source="lamport delta of party token accounts, net of wrapped-SOL "
            "balance",
        )
    recon["rent"] = Reconstructability.DERIVED

    # --- per-asset trade flow --------------------------------------------
    asset_deltas: dict[str, int] = {}
    decimals_by_mint: dict[str, Maybe[int]] = {}
    for pos in party_token_accounts:
        if pos.mint == WRAPPED_SOL_MINT:
            continue  # folded into the SOL leg below
        asset_deltas[pos.mint] = asset_deltas.get(pos.mint, 0) + pos.delta
        if pos.mint not in decimals_by_mint or isinstance(
            decimals_by_mint[pos.mint], Absent
        ):
            decimals_by_mint[pos.mint] = pos.decimals

    wsol_delta = sum(
        p.delta for p in party_token_accounts if p.mint == WRAPPED_SOL_MINT
    )

    sol_leg: Maybe[int]
    if isinstance(party_lamport_delta_raw, Absent) or isinstance(rent_net, Absent):
        sol_leg = absent(
            AbsenceReason.NOT_RECONSTRUCTABLE, "a SOL ledger component is unknown"
        )
    elif party_is_fee_payer and isinstance(total_fee, Absent):
        sol_leg = absent(
            AbsenceReason.NOT_RECONSTRUCTABLE,
            "party pays the fee but meta.fee is absent, so the fee cannot be "
            "removed from its lamport delta",
        )
    else:
        adjustment = Decimal(0)
        if party_is_fee_payer:
            adjustment += total_fee.value
        tip_from_party = sum(
            t.lamports
            for t in transfers
            if t.destination in tip_accounts and t.source in party_account_pubkeys
        )
        adjustment += Decimal(tip_from_party)
        adjustment += rent_net.value
        sol_leg = observed(
            int(Decimal(party_lamport_delta_raw.value) + adjustment) + wsol_delta,
            source="party lamport delta + fee + tip + rent + wrapped-SOL token delta",
        )
        if not parsed_ok:
            notes.append(
                "the SOL leg does not remove a tip because no tip could be "
                "identified; if a tip was paid the SOL cost is understated"
            )

    if isinstance(sol_leg, Observed) and sol_leg.value != 0:
        asset_deltas[NATIVE_SOL_ASSET] = sol_leg.value
        decimals_by_mint[NATIVE_SOL_ASSET] = observed(
            9, source="native SOL has 9 decimals (protocol constant)"
        )
    elif isinstance(sol_leg, Absent):
        notes.append("SOL leg not reconstructable; it is omitted, not zeroed")

    asset_deltas = {m: d for m, d in asset_deltas.items() if d != 0}

    # --- input / output ---------------------------------------------------
    actual_input: Maybe[TokenAmount]
    actual_output: Maybe[TokenAmount]
    negatives = {m: d for m, d in asset_deltas.items() if d < 0}
    positives = {m: d for m, d in asset_deltas.items() if d > 0}

    if not succeeded:
        reason_detail = "transaction failed; no asset changed hands, but fees did"
        actual_input = absent(AbsenceReason.TRANSACTION_FAILED, reason_detail)
        actual_output = absent(AbsenceReason.TRANSACTION_FAILED, reason_detail)
    elif not token_balances_provided:
        actual_input = absent(
            AbsenceReason.NOT_PROVIDED, "token balance arrays absent from meta"
        )
        actual_output = absent(
            AbsenceReason.NOT_PROVIDED, "token balance arrays absent from meta"
        )
    elif len(negatives) == 1 and len(positives) == 1:
        (in_mint, in_delta), = negatives.items()
        (out_mint, out_delta), = positives.items()
        actual_input = observed(
            TokenAmount(
                mint=in_mint,
                base_units=-in_delta,
                decimals=decimals_by_mint.get(
                    in_mint, absent(AbsenceReason.NOT_PROVIDED)
                ),
            ),
            source="balance-delta accounting",
        )
        actual_output = observed(
            TokenAmount(
                mint=out_mint,
                base_units=out_delta,
                decimals=decimals_by_mint.get(
                    out_mint, absent(AbsenceReason.NOT_PROVIDED)
                ),
            ),
            source="balance-delta accounting",
        )
    else:
        detail = (
            f"{len(negatives)} asset(s) decreased and {len(positives)} "
            f"increased for the party; this is not a single two-sided fill "
            f"(deltas={asset_deltas})"
        )
        actual_input = absent(AbsenceReason.NOT_RECONSTRUCTABLE, detail)
        actual_output = absent(AbsenceReason.NOT_RECONSTRUCTABLE, detail)
        notes.append(detail)
    recon["actual_input"] = Reconstructability.DERIVED
    recon["actual_output"] = Reconstructability.DERIVED

    costs = CostBreakdown(
        network_fee_lamports=base_fee,
        priority_fee_lamports=priority_fee,
        tip_lamports=tip.tip_lamports,
        compute_units_consumed=cu_consumed,
        compute_unit_price_micro_lamports=budget.unit_price_micro_lamports,
        rent_lamports_net=rent_net,
        tip_destinations=tip.destinations,
    )

    return DecodedTransaction(
        signature=signature,
        slot=slot,
        block_time=block_time,
        succeeded=succeeded,
        err=err_m,
        fee_payer=fee_payer,
        party=resolved_party,
        party_is_fee_payer=party_is_fee_payer,
        asset_deltas=asset_deltas,
        decimals_by_mint=decimals_by_mint,
        actual_input=actual_input,
        actual_output=actual_output,
        costs=costs,
        route=_candidate_route(top, every),
        party_lamport_delta_raw=party_lamport_delta_raw,
        transfers=tuple(transfers),
        token_positions=tuple(positions),
        reconstructability=recon,
        notes=tuple(notes),
    )


def realized_price(decoded: DecodedTransaction) -> Maybe[Decimal]:
    """Output per unit input, in human units.

    Absent whenever either leg is absent OR either scale is unknown — a price
    computed from base units of different decimal scales is off by a power of
    ten and looks entirely reasonable.
    """
    if isinstance(decoded.actual_input, Absent):
        return absent(decoded.actual_input.reason, "input leg absent")
    if isinstance(decoded.actual_output, Absent):
        return absent(decoded.actual_output.reason, "output leg absent")
    in_dec = decoded.actual_input.value.to_decimal()
    out_dec = decoded.actual_output.value.to_decimal()
    if isinstance(in_dec, Absent):
        return absent(in_dec.reason, "input decimals unknown")
    if isinstance(out_dec, Absent):
        return absent(out_dec.reason, "output decimals unknown")
    if in_dec.value == 0:
        return absent(
            AbsenceReason.NOT_RECONSTRUCTABLE, "zero input; price undefined"
        )
    return observed(
        out_dec.value / in_dec.value, source="actual_output / actual_input"
    )
