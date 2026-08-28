# SOLANA-READONLY-CHAIN-ADAPTER-001

**Status: BUILT AND LIVE-QUALIFIED, NOT MERGED.** Branch
`solana-readonly-chain-adapter-001`, held off `main` while S06 is armed against
a pinned commit.

Replaces Gate 1's injected `AccountReader` with a live implementation **without
increasing capability**.

## The capability surface is a list, not a transport

Five typed reads: `get_account_info` · `get_transaction` ·
`get_signature_status` · `get_slot` · `get_block_time`.

**There is no `call(method, params)`.** A generic escape hatch is convenient and
makes capability creep unauditable: once it exists, "read-only" is a property of
caller discipline rather than of the module. With an enumerated whitelist,
adding a write is a visible diff in one file.

Structurally absent and asserted: signer, private key, wallet, transaction
builder, `sendTransaction`, `simulateTransaction`, `requestAirdrop`, swap or
routing, block-engine submission. The class surface is **exactly** the five
reads — a test asserts set equality, which is tighter than a denylist.

Three capability mutations die: adding a generic `call()`, whitelisting
`sendTransaction`, and turning an RPC error into a null result.

## Live qualification against known chain objects

| object | verdict |
|---|---|
| SPL mint (USDC) | `CHAIN_VERIFIED`, decimals 6 |
| **Token-2022 mint (PYUSD)** | **`CHAIN_VERIFIED`**, decimals 6 |
| system program | `UNKNOWN_TOKEN_PROGRAM` (owner `NativeLoader…`) |
| token program account | `UNKNOWN_TOKEN_PROGRAM` (owner `BPFLoaderUpgradeab1e…`) |
| nonexistent address | `NOT_FOUND` |
| malformed pubkey | `CHAIN_INVALID` |
| **unreachable node** | **`UNAVAILABLE`, never `NOT_FOUND`** |

The last row is the standing positive control, checked against both a simulated
failure and a genuinely bad endpoint.

## The defect live qualification found

**Every constructed Token-2022 fixture was base-length (82 bytes), so the strict
`length != 82` check passed the entire suite while rejecting every *real*
extended mint.** PYUSD's mint account is **866 bytes** and was refused as
`WRONG_ACCOUNT_TYPE`.

Token-2022 accounts carry TLV extensions after the base layout, padded to 165
bytes with a discriminator byte at that offset — `1` = Mint, `2` = Account. So
**length no longer discriminates a token from somebody's balance of it; the
discriminator byte does.** Base-length accounts still use the length rule.

This is exactly what live qualification exists for: the fixtures were
self-consistent and unrepresentative. Three regression tests now cover extended
mint, extended token account, and unknown extended type, and three mutations on
that path die.

## A process note

The first attempt at this fix **silently did nothing**: a `str.replace` with no
`assert` on a pattern that had already been edited earlier, so it matched
nothing and reported success. The live run then showed the old behaviour and
looked like the fix had failed on its merits. Every source edit in this project
should assert its target exists — a no-op edit that reports success is the same
silent-wrongness class as a vacuous test pass.

## Not done

* No subscriptions. `logsSubscribe`/`accountSubscribe` are a later decision.
* No provider beyond the public endpoint; no paid tier evaluated.
* Nothing consumes this from the social pipeline yet — the next milestone is
  `SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001`, which measures the funnel and
  **is not an alpha test**.
