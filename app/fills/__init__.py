"""REALIZED-FILL-CORPUS-001 — machinery for RECORDING realized fills.

This package decodes already-confirmed, already-public Solana transactions and
normalizes them into a typed fill record. It is a measurement instrument.

Hard boundary (docs/SAFETY_BOUNDARIES.md, AGENTS.md "Forbidden capabilities"):
no capital, no order submission, no route execution, no transaction
construction, no simulation against an RPC node, no signing, no broadcasting,
no key material, no blockhash/priority-fee/nonce retrieval. The single network
verb reachable from this package is a read-only historical `getTransaction`
against a free public RPC endpoint. See
`docs/milestones/REALIZED-FILL-CORPUS-001.md` §9 and §10 for the written
boundary and what a future authorization would have to specify.
"""
