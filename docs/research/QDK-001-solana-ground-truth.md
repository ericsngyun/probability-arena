# QDK-001 — Is read-only realized-execution ground truth obtainable on Solana?

**Status: RESEARCH ONLY. SKELETON — sections filled in and committed one at a
time.** No production code, no flag, no migration, no schema change, no
deployment, no provider call, no RPC call. Nothing here is implemented and
nothing here authorizes a milestone.

Branch: `QDK-001-solana-ground-truth`. Base HEAD: `a5157be`.

---

## 0. The question, and the claim under test

`docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §8.2 states, as a permanent
limitation:

> **Realized slippage** (6) was the one quantity that could have validated a
> fill model against something resembling ground truth. The amendment closes the
> only route to it. **This milestone therefore has no ground truth to validate
> against and cannot acquire one within its boundary.** That is a permanent
> limitation of the result, not a gap more work will close.

and §8.1 row 6 classifies realized slippage as:

> **NOT OBSERVABLE prospectively.** Retrospective measurement needs a per-trade
> feed, which is **paid and now explicitly forbidden** (§3.2 F9)

The proposition under test is that the second sentence contains a hidden
premise — *"needs a per-trade feed"* — and that the premise is false, because a
per-trade feed is a **convenience layer over data the chain already publishes
for free**. Every swap on Solana is a confirmed transaction; `getTransaction`
returns its fee and its pre/post balances; a realized execution price is a
subtraction over those balances.

The claim is therefore a chain of six links, and it is worth exactly as much as
its weakest one:

| link | claim | where tested |
|---|---|---|
| L1 | `getTransaction` returns fee + pre/post SOL and token balances on a free public endpoint | §3 |
| L2 | swaps on the memecoin venues are identifiable without a paid decoder | §4 |
| L3 | **pre-trade pool state is recoverable read-only** | §5 |
| L4 | the data is available far enough back, or prospectively | §6 |
| L5 | free rate limits permit a useful collection rate | §7 |
| L6 | MEV/sandwich contamination is detectable rather than silently absorbed | §8 |

L3 is the crux. Realized price alone is a number with no key: a fill model maps
*(pre-trade state, size) → price*, so a corpus of prices without the state that
produced them cannot calibrate anything. If L3 fails, the rest is decoration.

**Terminology, fixed here so it is not blurred later.** This document is about
**third-party realized execution** — what price *someone else* got, on a public
chain, in a transaction they already sent. It is not about our fills, and it
does not become about our fills at any point. That distinction is what keeps the
whole enquiry inside the boundary (§1) and it is also the source of the enquiry's
single largest methodological weakness (§9).

## 1. Boundary — what this document may and may not propose

The governing text is `docs/SAFETY_BOUNDARIES.md`, amendment
**SAFETY-BOUNDARY-ROUTE-QUOTE-001 (2026-08-14)**. Quoted, not paraphrased:

> Under this mode there is **no implementation surface** for:
>
> - requesting, fetching, or receiving **swap instructions, a serialized
>   transaction, or transaction/instruction bytes** from any endpoint —
>   including the build/swap sibling route of the very API that served the
>   quote. Reaching a quote route grants nothing on any other route;
> - **constructing, assembling, encoding, or serializing** a transaction,
>   instruction, or message by any means, client-side included;
> - **simulating a transaction against an RPC node** (`simulateTransaction` and
>   equivalents) — that is transaction construction with a different verb, and
>   it requires the bytes this amendment forbids obtaining;
> - **signing** anything, with any key;
> - **submitting, broadcasting, sending, or relaying** a transaction, or
>   fetching a blockhash, priority fee, or nonce for one;
> - **loading, deriving, generating, importing, holding, or referencing wallet
>   key material**, seed phrases, or keypairs. […]
> - supplying a wallet address we control as the quote's user/payer, or any
>   parameter whose only function is to bind the quote to **our** ability to
>   execute it. The permitted object is *what a trade of size X would cost*,
>   never *the trade we are about to make*.

and:

> Neither mode may use a **paid RPC endpoint**, a **paid trade/orderflow feed**,
> or **SolanaTracker** — free public endpoints only. A route quote obtainable
> only by paying for it is not obtainable under this amendment; the correct
> outcome is no quote, reported honestly, never a purchase.

### 1.1 Why reading confirmed history is not adjacent to any of that

Every prohibition above is about a transaction **we** would cause to exist. This
document proposes reading transactions that **already exist and already
executed**, authored by unrelated parties, using methods (`getTransaction`,
`getSignaturesForAddress`, `getBlock`) whose entire semantic is *retrieval of
settled history*. Nothing here:

- constructs, encodes, or serializes anything — a `getTransaction` **response**
  is not transaction bytes we assembled and is not addressed to any signer;
- signs, submits, broadcasts, or relays;
- fetches a blockhash, a priority fee, or a nonce. **This is a real constraint,
  not a formality**: it means `getLatestBlockhash` and
  `getRecentPrioritizationFees` are out, and §5.3's fee decomposition must
  therefore stop where it stops;
- touches key material, a wallet, a seed, or a keypair;
- binds anything to our ability to execute. We never appear in the data.

**`simulateTransaction` is not used, not proposed, and not needed.** It is
banned by name in `docs/SAFETY_BOUNDARIES.md` and this design does not want it:
its whole appeal is predicting a *hypothetical* fill, whereas this document is
about reading *realized* ones. The banned method estimates the future; the
proposed methods read the past.

### 1.2 The one prohibition that genuinely bites

**No paid RPC.** Everything below is conditional on free public endpoints, and
§6 and §7 are where that condition does real damage — it is the reason §11's
design is prospective rather than a backfill. Where something is only obtainable
by paying, this document says *not obtainable*, per the amendment's own
instruction, and does not soften it.

### 1.3 What this document is not

It is **research only**. It proposes no code, no flag, no migration, no schema,
no timer, no deployment, and no call. It authorizes no milestone. §11 is a
*design sketch* to be judged, not a plan to be executed, and it would need its
own accepted milestone, its own checkpoints, and — per the amendment's
"Interaction with the AST safety audit" section — its own separately reviewed
`BANNED_IDENTIFIER_FRAGMENTS` decision, since an implementation naming `swap` in
the obvious way will fail `frontier-eval-report --include-safety` by design.

Note also that a corpus of third-party realized fills is **evidence**, never a
recommendation, and the moment it is used to produce a modeled number the
`PAPER_SIMULATION` requirements attach in full: an explicit model identifier and
an explicit modeled-vs-observed basis, **on the artifact itself**.

## 2. VERIFIED / INFERRED discipline

Every substantive claim below carries one of three tags.

| tag | meaning |
|---|---|
| **VERIFIED** | read in official Solana/Anza documentation, or in the Agave validator source, in this session. The URL is in §14 and the quoted text is quoted. |
| **INFERRED** | a conclusion I drew from VERIFIED facts. The inference is shown so it can be attacked. It is not a measurement. |
| **UNVERIFIED** | plausible, commonly asserted, or drawn from third-party/secondary sources, and **not** confirmed against a primary source in this session. §13 lists the check that would settle it. |

**No RPC call was made by this agent.** Nothing here is a measurement of live
mainnet behaviour. Claims about what a specific endpoint does *right now* —
notably retention (§6) and effective rate limits (§7) — are documentation and
architecture claims, and §13 states the exact human-runnable check for each.

---

## 3. Link 1 — what `getTransaction` actually returns

### 3.1 The method (VERIFIED)

`getTransaction(signature, config)` — `config.encoding` ∈ `{json, jsonParsed,
base58, base64, binary}`, `config.commitment` ∈ `{confirmed, finalized}`
(**`processed` is not accepted** — VERIFIED), and
`config.maxSupportedTransactionVersion` whose only valid value is `0`.

> "If you omit this parameter, only legacy transactions will be returned — any
> block containing a versioned transaction will result in an error."
> — `getBlock` docs, same parameter semantics

**This is a real trap, not a footnote.** Aggregator-routed swaps are
overwhelmingly v0 transactions using address lookup tables. A collector that
omits `maxSupportedTransactionVersion: 0` does not fail loudly — for
`getTransaction` it errors on that signature, and for `getBlock` it errors on
the whole block — and a naive retry-and-skip loop would silently build a corpus
biased *away from exactly the routed swaps that matter most*. (INFERRED from the
VERIFIED parameter semantics.)

`getTransaction` returns `null` "if transaction is not found or not confirmed at
the requested commitment level" (VERIFIED). **`null` is overloaded**: it means
*not found*, *not yet confirmed*, **and** *pruned* indistinguishably. See §6.4.

### 3.2 The `meta` fields this enquiry needs (VERIFIED, quoted)

| field | official text |
|---|---|
| `fee` | "Fee charged for the transaction, in lamports." |
| `preBalances` | "Lamport balances from before the transaction was processed, indexed to the transaction's full account key list." |
| `postBalances` | "Lamport balances from after the transaction was processed, indexed to the transaction's full account key list." |
| `preTokenBalances` | "List of token balances from before the transaction was processed. `null` if token balance recording was not available during this transaction." |
| `postTokenBalances` | "List of token balances from after the transaction was processed. `null` if token balance recording was not available." |
| `innerInstructions` | "List of inner instructions. […] `null` if inner instruction recording was not enabled during this transaction." |
| `logMessages` | "Program log output captured during execution. […] `null` if log recording was not enabled during this transaction." |
| `err` | "Transaction error. `null` indicates success." |
| `computeUnitsConsumed` | "Number of compute units consumed by the transaction." |
| `loadedAddresses` | "Loaded account addresses resolved from lookup tables. Present in full non-`jsonParsed` transaction metadata responses." |

Token-balance element schema (VERIFIED): `accountIndex` ("Index of the account
in which the token balance is provided for"), `mint` ("Pubkey of the token's
mint"), `owner` ("Pubkey of token balance's owner. **Omitted if the validator
did not record it**"), `programId` ("Pubkey of the Token program that owns the
account. **Omitted if the validator did not record it**"), and `uiTokenAmount`
carrying `amount` (**raw tokens as a string**), `decimals`, `uiAmount`
(*deprecated float*), `uiAmountString`.

### 3.3 Four consequences that constrain any honest implementation

**(a) `amount` is the only safe numeric, and `uiAmount` is a float.** The raw
`amount` is a decimal **string** of base units; `uiAmount` is documented as a
deprecated float. This repository has already been burned by exactly this class:
KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 found "silent decimal truncation of ordinary
venue JSON under a valid digest". Any collector must parse `amount` as an exact
integer and `decimals` as an integer, and must never touch `uiAmount`. The
existing `app/realtime/canonical.py` (`parse_float=Decimal`, `parse_int=int`)
already solves this and should be reused rather than re-solved. (VERIFIED field
types; INFERRED requirement.)

**(b) Four of the needed fields are contractually nullable.** `preTokenBalances`,
`postTokenBalances`, `innerInstructions` and `logMessages` may each be `null`,
and `owner`/`programId` may be *omitted per element*. On current mainnet these
are recorded in practice, but the **API contract permits their absence**, so
each must map to a typed absence in the sparse-lane sense — never to `0`, and
never to "assume the balance did not change". A `null` `preTokenBalances` on a
swap means *that swap is unusable*, not *that swap moved no tokens*. (VERIFIED
nullability; INFERRED requirement.)

**(c) Failed transactions are still returned, and still charged a fee.** `err`
is non-null on failure and `fee` is charged regardless. A realized-price corpus
must filter `err == null` before computing anything; a failed swap has a fee and
a signature but no fill. (VERIFIED.)

**(d) Balance indices are into the *full* key list, and resolving them requires
`loadedAddresses`.** `preBalances`/`postBalances` are "indexed to the
transaction's full account key list", which for a v0 transaction includes
addresses loaded from lookup tables. The Agave source fixes the order
explicitly:

> "Returns the address of the account at the specified index of the list of
> message account keys constructed from static keys, **followed by dynamically
> loaded writable addresses, and lastly the list of dynamically loaded readonly
> addresses**." — `anza-xyz/solana-sdk`, `message/src/account_keys.rs`

so index resolution is `static ++ loaded.writable ++ loaded.readonly`, in that
order, and getting it wrong attributes lamport deltas to the wrong accounts
without any error. **The token side is immune to this**, because each
`preTokenBalances` element carries its own `mint` and `owner` and needs no index
resolution at all — which is a good reason to build the primary measurement on
token balances rather than on lamport indices. (VERIFIED order; INFERRED
design consequence.)

### 3.4 Which fields survive on a *free public* endpoint

**All of them.** (INFERRED, with a stated basis.) The public endpoint is a
normal Agave RPC node; `meta` content is produced by the validator's transaction
recording, not by a commercial enrichment layer, and the official RPC reference
draws no distinction between free and paid nodes in the response schema. What a
free endpoint restricts is **rate** (§7) and **how far back** the node still
holds the slot (§6) — not which fields come back for a slot it does have.

One caveat that is genuinely operator-dependent: `logMessages` can be
**truncated**. Agave exposes `--log-messages-bytes-limit`, "Maximum number of
bytes written to the program log before truncation" (VERIFIED, from
`validator/src/cli.rs`), so a busy transaction's logs may be cut. Balances are
not subject to any such limit. **This is the first of several reasons §4 builds
venue and swap identification on balances and program IDs rather than on log
parsing.**

## 4. Link 2 — identifying swaps per venue without a paid decoder

*(pending)*

## 5. Link 3 — THE CRUX: is pre-trade pool state recoverable read-only?

*(pending)*

## 6. Link 4 — retention and availability on free endpoints

*(pending)*

## 7. Link 5 — rate limits and a sustainable prospective collection rate

*(pending)*

## 8. Link 6 — is MEV / sandwich activity detectable read-only?

*(pending)*

## 9. Selection bias — you observe the sizes traders chose

*(pending)*

## 10. VERDICT

*(pending)*

## 11. Prospective collection design (only if the verdict is YES)

*(pending)*

## 12. What this means for SOLANA-ROUTE-OBSERVATION-001

*(pending)*

## 13. Open questions and human-run checks

*(pending)*

## 14. Sources

*(pending)*
