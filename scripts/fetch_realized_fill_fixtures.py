#!/usr/bin/env python
"""Fetch and pin the REALIZED-FILL-CORPUS-001 fixtures (doctrine 9).

READ-ONLY. This script issues exactly one RPC method per fixture —
`getTransaction`, against already-confirmed history on the free public
mainnet endpoint (doctrine 17, tier 0). It cannot construct, simulate, sign or
submit anything; `SolanaRpcAdapter` refuses every such method by allowlist.

Usage:

    python scripts/fetch_realized_fill_fixtures.py            # refresh all
    python scripts/fetch_realized_fill_fixtures.py --check    # verify only

The signature list is PINNED below and is not discovered at run time. Fixture
selection is a reviewable decision: re-running this must reproduce the same
corpus, and a script that goes looking for "a recent transaction" would give a
different one every time and quietly change what the suite certifies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.adapters.solana_rpc import (  # noqa: E402
    DEFAULT_RPC_URL,
    SolanaRpcAdapter,
)
from app.fills.provenance import (  # noqa: E402
    FIXTURE_SCHEMA_VERSION,
    MANIFEST_NAME,
    FixtureProvenance,
    content_hash,
    load_fixture_set,
    semantic_hash,
    verify_offline,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "solana_fills"

RETRIEVED_BY = "scripts/fetch_realized_fill_fixtures.py (REALIZED-FILL-CORPUS-001)"

#: capture_id -> (signature, party_override, hard_cases, selection_reason)
#:
#: Every one of these was found by walking `getSignaturesForAddress` over
#: public block-engine tip accounts and classifying the results; the walk is
#: not part of this script because the corpus must be reproducible.
PINNED: dict[str, dict] = {
    "direct_dispose_wrapped_sol_ata_cycle": {
        "signature": (
            "4FcuJFeixgzgoefRb2FE1hzYKWCjpTaX28Ui4NRdkj1XnwkVhcCd3KNbgq4wufJ3"
            "4KxdgkogsC3d843kBQ8UWSKQ"
        ),
        "party": None,
        "hard_cases": (
            "wrapped_sol",
            "ata_creation_and_closure_rent",
            "fee_payer_is_trade_party",
            "versioned_transaction_lookup_table",
            "mev_tip",
        ),
        "selection_reason": (
            "A one-hop route that disposes an SPL token for SOL, wrapping "
            "through a WSOL associated token account that is created "
            "(createIdempotent) and closed (closeAccount) inside the same "
            "transaction. The wrapped-SOL account therefore never appears in "
            "pre/postTokenBalances, so the entire proceeds must come from the "
            "lamport ledger with the fee and the tip added back. It is also "
            "the POSITIVE CONTROL for log parsing: with a single hop the "
            "balance-delta answer and the instruction operand must agree "
            "exactly, and they do (316,053,825 lamports)."
        ),
    },
    "direct_dispose_no_ata_creation": {
        "signature": (
            "3zsiTE5P3jCVyoRv2uF6Eq8dKfC9gS1Gu9s9WwiUtCT2d2mUGuXr5LJ34EU1n64j"
            "gFWKRTQyjkJh3m9sES3Zsw84"
        ),
        "party": None,
        "hard_cases": (
            "wrapped_sol",
            "fee_payer_is_trade_party",
            "versioned_transaction_lookup_table",
            "mev_tip",
        ),
        "selection_reason": (
            "A second direct dispose with no ATA creation, so the rent term "
            "is a MEASURED zero rather than an unexercised one. Without it "
            "'rent = 0' in the primary fixture proves nothing about the rent "
            "path."
        ),
    },
    "multi_hop_cyclic_route": {
        "signature": (
            "SFr9cfiYkEfuEFniLxmjDk4tPnAUJKMPaQrhcxYU3pMpwz7UzeztqZPsasZWAbQW"
            "bqFdjynhnRXTbWpw5WjYFR5"
        ),
        "party": None,
        "hard_cases": (
            "multi_hop_route",
            "naive_log_parse_is_wrong",
            "wrapped_sol",
            "fee_payer_is_trade_party",
            "versioned_transaction_lookup_table",
            "mev_tip",
        ),
        "selection_reason": (
            "THE NEGATIVE CONTROL FOR LOG PARSING. A two-hop cyclic route "
            "through two pools: USDC -> WSOL -> USDC. The instruction "
            "operands report 107,232,267 and 107,232,992 USDC and "
            "1,228,043,049 WSOL, while the party's true net position change "
            "is +725 USDC base units. A parser that reads the last "
            "transferChecked amount is wrong by a factor of ~148,000. "
            "Balance-delta accounting gets it right because the intermediate "
            "asset nets to exactly zero for the trading party."
        ),
    },
    "multi_hop_counterparty_view": {
        "signature": (
            "SFr9cfiYkEfuEFniLxmjDk4tPnAUJKMPaQrhcxYU3pMpwz7UzeztqZPsasZWAbQW"
            "bqFdjynhnRXTbWpw5WjYFR5"
        ),
        "party": "8ekCy2jHHUbW2yeNGFWYJT9Hm9FW7SvZcZK66dSZCDiF",
        "hard_cases": ("fee_payer_is_not_trade_party", "multi_hop_route"),
        "selection_reason": (
            "The SAME real transaction decoded from a COUNTERPARTY's "
            "perspective — one of the two pool accounts. That account is a "
            "real trade party with real balance deltas and it did not pay the "
            "fee, which exercises the fee-payer-is-not-the-party branch on "
            "real data instead of on a fabricated one. It shares a stored "
            "payload with multi_hop_cyclic_route by design; the case is the "
            "(transaction, party) pair, not the transaction."
        ),
    },
    "failed_transaction_legacy_high_priority": {
        "signature": (
            "4pqbgr92TafgYYfoCNooWsiGyYRQCgUtU8tKk8yDXYVbZ65LLuMfrXPDzcMxft8H"
            "iDUgtncuNqKRv2yLTWqL3WdC"
        ),
        "party": None,
        "hard_cases": (
            "failed_transaction",
            "priority_fee_nonzero",
            "reverted_tip_intent",
            "legacy_transaction",
        ),
        "selection_reason": (
            "A FAILED legacy transaction (InstructionError Custom 6061) that "
            "still paid 605,000 lamports: 5,000 base + 600,000 priority. Its "
            "instructions asked to transfer 1,500,000 lamports to a tip "
            "account and 260,000,000 to another account, and BOTH reverted — "
            "the destinations' balances moved 0. This fixture is why the "
            "decoder reads tips from balance deltas rather than operands."
        ),
    },
    "failed_transaction_v0_high_priority": {
        "signature": (
            "4VFstbamwApnhvBcha7c9SZ4W5TqEKrT8CymLR1fNssySZF22DhRSZY2GPZu9a8e"
            "wrLgUcYZDNLn6TbncPhuqtV"
        ),
        "party": None,
        "hard_cases": (
            "failed_transaction",
            "priority_fee_nonzero",
            "reverted_tip_intent",
            "priority_fee_charged_on_requested_limit",
            "versioned_transaction_lookup_table",
        ),
        "selection_reason": (
            "A FAILED v0 transaction that paid 1,005,000 lamports while "
            "consuming only 4,919 compute units at a unit price of 3,333,333 "
            "micro-lamports. price x CONSUMED would be 16,398 lamports; price "
            "x REQUESTED LIMIT (300,000) is 1,000,000, which matches the "
            "meta.fee residual exactly. This is the empirical refutation of "
            "the 'priority fee = price x units consumed' formulation."
        ),
    },
}


async def fetch_all(check_only: bool) -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    adapter = SolanaRpcAdapter(timeout=30.0)

    if check_only:
        fixtures = load_fixture_set(FIXTURE_DIR)
        problems = verify_offline(fixtures)
        for p in problems:
            print("VIOLATION:", p)
        print(f"{len(fixtures.entries)} fixtures, {len(problems)} violations")
        return 1 if problems else 0

    genesis = await adapter.get_genesis_hash()
    if genesis is None:
        print("could not read genesis hash; refusing to pin a fixture "
              "without chain identity")
        return 2

    entries: list[FixtureProvenance] = []
    # Distinct stored payloads, keyed by signature: two capture ids may share
    # one transaction when they differ only by the party being measured.
    written: dict[str, str] = {}

    for capture_id, spec in PINNED.items():
        signature = spec["signature"]
        if signature in written:
            relative = written[signature]
            raw = (FIXTURE_DIR / relative).read_bytes()
            payload = json.loads(raw)
            retrieved_at = "shared-with:" + relative
        else:
            payload = await adapter.get_transaction(signature)
            await asyncio.sleep(0.8)
            if not isinstance(payload, dict):
                print(f"FETCH FAILED for {capture_id} ({signature[:16]}…)")
                return 3
            relative = f"{signature}.json"
            raw = json.dumps(payload, sort_keys=True, indent=1).encode()
            (FIXTURE_DIR / relative).write_bytes(raw)
            written[signature] = relative
            retrieved_at = datetime.now(timezone.utc).isoformat()

        stored_raw = (FIXTURE_DIR / relative).read_bytes()
        entries.append(
            FixtureProvenance(
                capture_id=capture_id,
                venue="solana-mainnet-beta",
                chain_genesis_hash=genesis,
                signature=signature,
                slot=int(payload.get("slot") or 0),
                block_time=payload.get("blockTime"),
                rpc_endpoint=DEFAULT_RPC_URL,
                rpc_method="getTransaction",
                rpc_encoding="jsonParsed",
                rpc_commitment="confirmed",
                rpc_max_supported_version=0,
                retrieved_at=retrieved_at,
                retrieved_by=RETRIEVED_BY,
                schema_version=FIXTURE_SCHEMA_VERSION,
                content_sha256=content_hash(stored_raw),
                semantic_sha256=semantic_hash(json.loads(stored_raw)),
                hard_cases=tuple(spec["hard_cases"]),
                selection_reason=spec["selection_reason"],
                relative_path=relative,
                expected={"party": spec["party"]},
            )
        )
        print(f"pinned {capture_id}: {relative}")

    manifest = {
        "milestone": "REALIZED-FILL-CORPUS-001",
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "generated_by": RETRIEVED_BY,
        "note": (
            "Read-only capture of already-confirmed public mainnet "
            "transactions. NONE of these is ours: this system has never "
            "traded, holds no key material and can submit nothing. They are "
            "third-party transactions used as decoder ground truth."
        ),
        "fixtures": [e.to_json() for e in entries],
    }
    (FIXTURE_DIR / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=1, sort_keys=False) + "\n"
    )
    print(f"wrote {MANIFEST_NAME} with {len(entries)} fixtures")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify pinned hashes offline; make no network call",
    )
    args = parser.parse_args()
    return asyncio.run(fetch_all(args.check))


if __name__ == "__main__":
    raise SystemExit(main())
