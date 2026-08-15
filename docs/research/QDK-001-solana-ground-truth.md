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

### 4.1 The framing that makes this easy, and why the obvious framing is wrong

The obvious approach is: enumerate venue program IDs, decode each venue's swap
instruction with its IDL, read the amounts out of the instruction data. That
approach is what makes people buy a decoder, and it has three failure modes that
all point the same way: it needs an IDL per venue, it breaks when a venue
upgrades its instruction layout, and — worst — **it needs an allowlist, so any
venue you failed to enumerate is silently invisible.** On a memecoin tape where
new venues appear continuously, an allowlist-shaped detector produces a corpus
whose *absences* are undetectable.

**Invert it.** A swap is not defined by which program ran it. A swap is defined
by what moved:

> In one confirmed transaction, one party's holding of mint A decreased and
> their holding of mint B increased, and some counterparty account set moved the
> opposite way.

That is entirely a statement about `preTokenBalances` / `postTokenBalances` and
`preBalances` / `postBalances`. It requires **no instruction decoding, no IDL,
no discriminator table, and no allowlist** — and it therefore detects swaps on
venues nobody has heard of. (INFERRED, from the VERIFIED field semantics in §3.)

### 4.2 The detector, stated concretely

For a transaction with `err == null`:

1. Build `token_delta[(owner, mint)] = post.amount − pre.amount` over
   `postTokenBalances` ∪ `preTokenBalances`, using the raw `amount` strings as
   exact integers. An account present in only one list is an open or close and
   its missing side is `0` **for that account's own balance** — this is the one
   place a zero is a fact rather than an assumption, because an SPL token
   account that does not exist holds nothing.
2. Build `lamport_delta[account] = postBalances[i] − preBalances[i]`, resolving
   `i` as `static ++ loaded.writable ++ loaded.readonly` (§3.3d). Wrapped SOL
   appears on the token side instead; native SOL appears here.
3. The **taker** is the fee payer (account index 0) in the common case, or more
   robustly, the signer whose deltas form exactly one negative/one positive mint
   pair. `mint_in` is the mint they lost, `mint_out` the mint they gained.
4. The **counterparty vault set** is the accounts whose deltas are
   equal-and-opposite in the same two mints and which are **not** signers. Their
   `owner` field (from the token-balance element) is the pool authority PDA.

`token_delta` sums must net to zero per mint across the transaction **except**
for mints with transfer fees or mint/burn — which is itself the detector for the
Token-2022 transfer-fee case that
`docs/milestones/SOLANA-ROUTE-OBSERVATION-001.md` §8.1 row 5d flags as a
**silent-wrongness** risk. Under the balance method that risk stops being silent:
if the taker lost 100 and the vault gained 98, the 2 is measured, not assumed
away. **This is a strict improvement over the quote-based lane**, which can only
record 5d as an unresolvable absence. (INFERRED.)

### 4.3 Venue labelling is free, and is a *result* rather than an input

`jsonParsed` returns, for instructions it cannot decode, "partially decoded
objects with `accounts`, `data`, and `programId` fields" (VERIFIED, §3.2). So
**the program ID always comes back, whether or not anything can decode the
instruction.** Combined with `innerInstructions` — which is where the actual
token transfers of a routed swap live — you can attribute each swap to the
program that invoked the transfers, with zero decoding.

That turns venue coverage from an allowlist into a measurement: group the
collected corpus by `programId`, sort by count, and label the head by hand once.
A venue you have never heard of shows up as an unlabelled program ID with a
share of the tape — **visible, quantified, and honestly unnamed** — instead of
as a hole.

For reference, the majors' program IDs, with provenance stated because these
addresses are load-bearing and a wrong one silently mislabels a whole venue:

| venue | program ID | provenance |
|---|---|---|
| Raydium AMM v4 (hybrid + OpenBook) | `675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8` | **VERIFIED** — `docs.raydium.io/reference/program-addresses` |
| Raydium CPMM (standard AMM) | `CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C` | **VERIFIED** — same page |
| Raydium CLMM (concentrated) | `CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK` | **VERIFIED** — same page |
| Raydium LaunchLab | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | **VERIFIED** — same page |
| pump.fun bonding curve | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | **VERIFIED** — `pump-fun/pump-public-docs`, `PUMP_PROGRAM_README.md` |
| PumpSwap AMM | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | **UNVERIFIED** — secondary sources only in this session |
| Orca Whirlpool | `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc` | **UNVERIFIED** — secondary sources only in this session |
| Meteora DLMM | `LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo` | **UNVERIFIED** — secondary sources only in this session |

The three UNVERIFIED rows are **not a blocker**, precisely because §4.2 does not
depend on them: they are labels applied after detection, and §13 gives the
one-line check that confirms each against the chain itself.

### 4.4 Why not parse logs

Log parsing (`Program log: ray_log: …`, `Program data: <base64>` Anchor events)
is the other common route, and it is genuinely richer for some venues —
pump.fun's Anchor event, if present, would carry post-trade reserves directly.
It is rejected as the **primary** basis for three reasons:

1. **Logs are truncatable.** `--log-messages-bytes-limit`, "Maximum number of
   bytes written to the program log before truncation" (VERIFIED). A truncated
   log yields a *partial* parse, which is the fabrication shape this repository
   most wants to avoid — a plausible number derived from incomplete evidence.
2. **`logMessages` is contractually nullable** (VERIFIED, §3.2). Balances are
   the same contract, but a lane that depends on logs has two nullable
   dependencies instead of one.
3. **Log formats are venue-private and unversioned.** Balances are consensus
   state.

Logs remain useful as a **corroborating** channel — an independent second
derivation of the same fill that should agree with the balance derivation, and
whose disagreement is a bug signal. That is how they should be used if used at
all. (INFERRED.)

---

## 5. Link 3 — THE CRUX: is pre-trade pool state recoverable read-only?

**Answer: YES for constant-product venues, from the same transaction, with no
second call. PARTIAL for concentrated-liquidity venues. And the reason it works
is stronger than the proposal assumed.**

### 5.1 Why this looked hard

`getAccountInfo` has **no historical-slot parameter** (VERIFIED). Its
`minContextSlot` is "the minimum slot that the request can be evaluated at" — a
*freshness floor*, not a time machine. There is no read-only way to ask a
standard RPC node "what did this pool account contain at slot N in the past."

So the obvious route to pre-trade state — read the pool account as of the slot
before the swap — **does not exist on free infrastructure**, and does not exist
on paid infrastructure either without an account-state archive, which is a
distinct and expensive product. If pre-trade state required that, L3 would fail.

### 5.2 It does not require that, because the pre-state is inside the swap

**VERIFIED, from the Agave validator source** (`ledger/src/token_balances.rs`,
`collect_token_balances`):

```rust
for (index, account_id) in account_keys.iter().enumerate() {
    if transaction.message().is_invoked(index) || is_known_spl_token_id(account_id) {
        continue;
    }

    if let Some(TokenBalanceData { mint, ui_token_amount, owner, program_id }) =
        collect_token_balance_from_account(bank, account_id, mint_decimals)
    { … }
}
```

and `collect_token_balance_from_account` returns `Some(...)` for any account
whose owner is a known SPL token program.

Read that loop precisely. It iterates **every account key of the transaction** —
not only the signer's, not only writable ones — skipping only top-level invoked
program IDs and the token programs themselves, and records a balance for each
one that is an SPL token account.

**Therefore `preTokenBalances` contains the AMM's own vault balances, at the
instant immediately before this swap executed, in the same response that
reports the swap.** The pool's vaults are necessarily in the account key list —
a swap cannot debit a vault it did not reference — so they are necessarily
recorded.

This is a materially better result than the proposal's phrasing ("pair that with
pool reserve state immediately prior") suggests, and the difference matters:

- **No second RPC call.** Pre-state costs zero additional requests, which
  changes §7's rate arithmetic by roughly a factor of two.
- **No prior-slot lookup, and no dependence on prior-slot availability.**
- **Intra-block ordering is handled for free.** If three swaps hit the same pool
  in one block, each one's own `preTokenBalances` reflects the state after the
  preceding two, because balances are captured per transaction in execution
  order. A design that reconstructed state from "the last observation of the
  pool" would get all but the first of those wrong. This is also what makes §8's
  sandwich detection possible at all.
- **It is self-verifying.** `post = pre + delta` must hold on every vault. A
  violation means the parse is wrong, not that the chain is.

`preBalances` / `postBalances` do the same job for native SOL vaults and for
program-owned accounts that hold lamports rather than tokens, subject to the
index-resolution rule in §3.3d.

### 5.3 What "pre-trade state" means per venue — the honest split

The pre-trade *reserves* being observable is not the same as the pre-trade
*state* being sufficient. What a fill model needs is whatever quantity the pool
prices against.

| venue class | what prices the swap | recoverable from the transaction alone? |
|---|---|---|
| **Constant-product AMM** (Raydium AMM v4, Raydium CPMM, PumpSwap, Orca legacy pools) | the two vault balances, `x·y = k` | **YES, fully.** Both vaults are in `preTokenBalances`. Pre-trade mid, depth, and the theoretical impact of the observed size are all computable from the same response. |
| **pump.fun bonding curve** | `virtual_sol_reserves`, `virtual_token_reserves` — *synthetic* quantities in the curve account, **not** vault balances | **YES, derivably** — see §5.4. This is the case that looked fatal and is not. |
| **Concentrated liquidity** (Orca Whirlpool, Raydium CLMM) | in-range liquidity `L`, `sqrt_price`, and the tick array — i.e. the *shape* of liquidity, not its total | **NO, not fully.** Vault balances give total value locked, which does not determine the price curve. §5.5. |
| **Meteora DLMM** (discretized bins) | per-bin liquidity and the active bin | **NO, not fully.** Same reason. §5.5. |

### 5.4 pump.fun — the hard case, and why it resolves

pump.fun prices against *virtual* reserves, so vault balances alone are not the
state. But pump's own public documentation makes the derivation exact
(**VERIFIED**, `PUMP_PROGRAM_README.md`):

> "On each `buy` operation, `virtual_sol_reserves` and `real_sol_reverses`
> increase with the same lamports amount according to the bonding curve formula,
> while `virtual_token_reserves` and `real_token_reserves` decrease with the same
> coin amount."
>
> "On each `sell` operation, `virtual_sol_reserves` and `real_sol_reverses`
> decrease with the same lamports amount […] while `virtual_token_reserves` and
> `real_token_reserves` increase with the same coin amount."

The virtual and real reserves move by **identical** amounts, always. So virtual
reserves differ from real reserves by a constant fixed at curve creation:

```
virtual_sol_reserves   = real_sol_reserves   + initial_virtual_sol_reserves
virtual_token_reserves = real_token_reserves + (initial_virtual_token_reserves
                                               − initial_real_token_reserves)
```

and both `initial_*` constants live in the single `Global` account
`4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` (VERIFIED, PDA of `["global"]`),
readable by one free `getAccountInfo`. `real_sol_reserves` is tracked by the
bonding curve account's lamport balance, which is in `preBalances`;
`real_token_reserves` is tracked by the curve's token account, which is in
`preTokenBalances`.

**Result: the full pre-trade bonding-curve pricing state is recoverable from the
swap transaction plus two global constants.** (INFERRED from the VERIFIED
invariant; the derivation is arithmetic, not assumption.)

Three cautions, stated rather than glossed:

- The `Global` constants are **mutable** — `set_params` "allows updating all the
  `Global` account fields" (VERIFIED). They must be read and stamped with the
  slot at which they were read, and a corpus spanning a `set_params` change is
  wrong unless it is segmented. Treating them as compile-time constants is a
  silent-wrongness bug.
- The mapping from account **lamport balance** to `real_sol_reserves`, and from
  **token account balance** to `real_token_reserves`, involves fixed offsets
  (rent-exempt minimum; the migration reserve, i.e.
  `token_total_supply − initial_real_token_reserves`). Those offsets are
  **INFERRED**, not verified, and §13 gives the check that pins them: read one
  live bonding curve with `getAccountInfo` and compare its decoded fields
  against the balances the same transaction reported.
- A cleaner alternative exists and should be preferred if it validates: the
  program's own Anchor trade event, if emitted, carries the post-trade reserves
  directly. It is **UNVERIFIED** here — the public README documents no events —
  and it is log-borne, so §4.4's truncation objection applies. It is a
  corroborator, not a foundation.

### 5.5 Concentrated liquidity — the honest failure, and its size

For Orca Whirlpool, Raydium CLMM and Meteora DLMM, the pre-trade vault balances
*are* observable exactly as above, but they **do not determine the price
response**. Two pools with identical TVL and identical spot price can have
wildly different impact for the same size depending on how liquidity is
distributed across ticks or bins. The tick/bin arrays live in program-owned
accounts whose *pre-trade contents* are not in `preTokenBalances` (they are not
token accounts) and cannot be read historically (§5.1).

What remains recoverable for these venues, honestly:

- **`sqrt_price` / active bin immediately before the swap** — INFERRED as
  recoverable, because it is implied by the realized price of the *first*
  infinitesimal amount and, more practically, by chaining: the post-state of the
  previous swap on the same pool is the pre-state of this one, and a forward
  collector sees both. But this requires an unbroken chain from a known
  anchor and degrades on any gap.
- **The realized `(size → price)` point itself** — fully recoverable. You simply
  do not get to know *why*.

**So for CLMM/DLMM the corpus is a set of realized outcomes without their full
causal state.** That is a genuine, permanent limitation on free data, and it
must be recorded per-row as a typed state-completeness flag —
`pool_state_complete` vs `pool_state_partial_clmm` — rather than averaged into
the same bucket as the constant-product rows. **A calibration that pools them is
fitting a curve to points whose x-coordinate is partly unknown**, and it will
look better than it is.

The practical mitigation is not a workaround but a scoping decision: pump.fun
and constant-product Raydium/PumpSwap pools are where new memecoins actually
trade in their first hours, which is the population this project's sparse lane
already observes. **The venues where L3 fully succeeds are the venues that
matter for this project's population** — an alignment worth confirming
empirically (§13) rather than assuming.

### 5.6 The realized-price computation, field by field

For a constant-product or bonding-curve swap with `err == null`:

```
taker            = fee payer / the signer with exactly one (−mint_in, +mint_out) pair
amount_in        = −token_delta[(taker, mint_in)]        exact integer, base units
amount_out       = +token_delta[(taker, mint_out)]       exact integer, base units
realized_price   = amount_out / amount_in                exact rational, decimal-normalized
                                                          by both mints' `decimals`
network_fee      = meta.fee                              lamports, VERIFIED field
pool_pre_in      = preTokenBalances[vault_in].amount     exact integer
pool_pre_out     = preTokenBalances[vault_out].amount    exact integer
pre_mid          = pool_pre_out / pool_pre_in            (constant-product only)
realized_slippage_vs_pre_mid = realized_price / pre_mid − 1
slot             = result.slot
block_time       = result.blockTime
```

Notes that keep this honest:

- **`realized_price` is gross of the network fee and net of every in-protocol
  fee.** The pool's fee, the venue's platform fee, and any creator fee are all
  already inside `amount_out` because they were taken before the taker received
  anything. That is *the right thing* for calibrating an execution model: it is
  the all-in price the taker actually got. The proposal's formula, "(Δ quote
  token + fees) / (Δ base token)", would **double-count** those in-protocol fees
  if `fees` were read as the pool fee. `meta.fee` is the *network* fee only
  (VERIFIED: "Fee charged for the transaction, in lamports") and is a separate,
  additive, size-independent cost that belongs in a separate column, not inside
  the price.
- **`realized_slippage_vs_pre_mid` is not the same quantity as the slippage a
  trader experiences versus their own quote.** It is impact versus the pre-trade
  mid — which is exactly what an AMM fill model needs, and is *better* than
  quote-versus-fill for calibration because it has no dependence on when the
  trader fetched their quote. Calling it "realized slippage" without
  qualification would be an equivocation; the column should be named for what it
  is.
- **Priority fees are invisible here.** `meta.fee` covers the transaction fee.
  Jito-style tips are ordinary SOL transfers to a tip account and appear as
  lamport deltas, so they are *detectable*, but attributing them requires an
  allowlist of tip accounts. Recording them as an unattributed lamport outflow
  is honest; asserting a tip is not. And §1 forbids fetching a priority fee for
  a transaction of our own regardless. (INFERRED.)
- Every quantity above is an exact integer or an exact rational. Nothing needs a
  float, and per §3.3a nothing may use one.

## 6. Link 4 — retention and availability on free endpoints

### 6.1 The architecture (VERIFIED)

Historical transaction reads are not a core validator function; they are two
opt-in features on an RPC node, and their exact help text is worth having:

> `--enable-rpc-transaction-history` — "Enable historical transaction info over
> JSON RPC, including the 'getConfirmedBlock' API. This will cause an increase
> in disk usage and IOPS"
>
> `--enable-rpc-bigtable-ledger-storage` (`.requires("enable_rpc_transaction_history")`)
> — "Fetch historical transaction info from a BigTable instance **as a fallback
> to local ledger data**"
>
> — Agave, `validator/src/cli.rs`

So a node answers `getTransaction` from **local ledger first**, and only reaches
a BigTable archive if the operator configured one. Local ledger extent is
governed by `--limit-ledger-size` ("specify how many blocks to store on the RPC
node" — Anza operations docs) and is trimmed by the ledger cleanup service.

The error codes make the two tiers visible (**UNVERIFIED** in this session —
read from a secondary rendering of `rpc_custom_error.rs`, not from the primary
source):

| code | meaning |
|---|---|
| `-32001` | `BlockCleanedUp` — the node had it and purged it |
| `-32004` | `BlockNotAvailable` |
| `-32007` | `SlotSkipped` |
| `-32009` | `LongTermStorageSlotSkipped` — "Slot XXX was skipped, or missing in long-term storage" |

`-32009` existing at all is the tell that a BigTable fallback is a real,
separate tier: it is the *archive's* miss, not the node's.

### 6.2 How far back a free public endpoint serves — the honest answer

**Not documented, and therefore not assertable.** The official cluster reference
documents rate limits for `api.mainnet-beta.solana.com` in detail (§7) and says
**nothing** about retention. It does say:

> "The public RPC endpoints are not intended for production applications." /
> "The public services are subject to abuse and rate limits may change without
> prior notice."

Third-party sources put practical public-endpoint history at **roughly a few
days** (commonly repeated as "3–4 days"), which is consistent with a local
ledger sized by disk rather than by time. That figure is **UNVERIFIED** and this
document does not adopt it as a number. What *is* structurally certain:

1. Retention is an **operator configuration**, not a protocol guarantee, so it
   can change without notice and differs per node behind the same hostname.
2. `getTransaction` returns **`null`** for a pruned transaction, which is
   indistinguishable from *not found* and from *not yet confirmed* (VERIFIED).
   **A backfill collector cannot tell "this swap did not exist" from "this node
   forgot it."** For a corpus whose value depends on knowing its own
   denominator, that ambiguity is disqualifying on its own.
3. `getFirstAvailableBlock` — "Returns the lowest confirmed block slot still
   available in this node's ledger" (VERIFIED) — is the exact probe, and it is
   free. Any design touching history must call it and record the answer per
   pass, because it is the only self-reported statement of the horizon.

### 6.3 What an archival node changes, and why it is out of scope

An archival node (BigTable-backed or an Old-Faithful-style archive) extends
`getTransaction` and `getBlock` to the full chain history. It changes nothing
about the *schema* — the same `meta` fields come back — only the *reach*.

**Archival access is a paid product.** Under
`docs/SAFETY_BOUNDARIES.md` SAFETY-BOUNDARY-ROUTE-QUOTE-001, "Neither mode may
use a **paid RPC endpoint** […] — free public endpoints only", and the
amendment's own instruction for this situation is explicit: *"the correct
outcome is no quote, reported honestly, never a purchase."* So:

> **A retrospective backfill of historical swaps is NOT OBTAINABLE within the
> boundary.** Not "expensive". Not obtainable.

### 6.4 The forward-collected corpus makes the problem disappear — say so plainly

**Yes. Completely.** This is the decisive point of §6.

A prospective collector that reads slots shortly after they are produced never
touches the retention horizon at all. It is always reading data that is minutes
old on a node whose local ledger holds days. Every §6 hazard evaporates:

| hazard | prospective collector |
|---|---|
| pruning | reading minutes-old data, orders of magnitude inside any plausible horizon |
| `null` ambiguity | a `null` at t+2min means *not confirmed yet* — retryable, and distinguishable, because you know the slot exists |
| archival cost | zero; free endpoint suffices |
| unknown denominator | the denominator is *what you asked for*, recorded as you ask |
| operator config drift | `getFirstAvailableBlock` per pass detects it; a prospective lane is never near the boundary anyway |

And it is the same shape this repository has already been forced into twice:
`CRYPTO-COVERAGE-REPAIR-001` drained the historical recoverable pool in a single
pass (1,043 → 106) and `CRYPTO-COVERAGE-REPAIR-002` exists precisely because the
retrospective half was exhausted. **The lesson generalizes: on this project's
data sources, retrospective recovery is always smaller than it looks and
prospective collection is always the real instrument.** This is one more
instance, not a new discovery.

The cost of prospectivity is honest and should be stated: **no corpus exists on
day one.** A calibration corpus accumulates at the rate the tape produces
swaps, and the first useful sample is weeks away, not hours. That is a schedule
fact for Eric, not an engineering obstacle.

---

## 7. Link 5 — rate limits and a sustainable prospective collection rate

### 7.1 The documented limits (VERIFIED, quoted)

From the official cluster reference, applying to the public mainnet, devnet and
testnet endpoints alike:

> - "Maximum number of requests per 10 seconds per IP: **100**"
> - "Maximum number of requests per 10 seconds per IP for a single RPC: **40**"
> - "Maximum concurrent connections per IP: **40**"
> - "Maximum connection rate per 10 seconds per IP: **40**"
> - "Maximum amount of data per 30 second: **100 MB**"

and, from the RPC overview: "Shared public endpoints may return `429` when you
exceed rate limits and `403` when traffic is blocked."

Restated as rates: **10 req/s overall, 4 req/s for any single method, 3.33 MB/s
of response data.**

### 7.2 The binding constraint is bandwidth, not requests — and that kills the firehose

The tempting design is to scrape every block: `getBlock(slot,
transactionDetails: "full")` returns every transaction in a slot with full
`meta` in one request, which is enormously more request-efficient than
per-signature fetching.

The request budget permits it. Solana produces roughly 2.5 slots/s ≈ 216,000
slots/day, and the single-method cap of 4 req/s allows ~345,600 requests/day.
Requests are not the problem.

**Bandwidth is.** A mainnet block with full JSON metadata is on the order of
single-digit megabytes to tens of megabytes, so a full firehose needs somewhere
around 10–75 MB/s against a documented ceiling of 3.33 MB/s — **roughly one to
two orders of magnitude over budget.** (VERIFIED cap; block-size magnitudes are
**INFERRED** and are the weakest number in this section — §13 gives the one-shot
check that measures a real block's response size.)

**Conclusion: whole-chain collection is not available for free. Targeted
collection is.** That is not a disappointment; it is the design constraint that
produces the right architecture.

### 7.3 The targeted design, and its actual arithmetic

Scope collection to the tokens this project already tracks — the sparse
observer's rolling cohort — rather than to mainnet:

1. `getSignaturesForAddress(address)` — up to **1,000** signatures per call
   (VERIFIED: "Maximum transaction signatures to return (between 1 and 1,000)"),
   with `until: <last signature seen>` for incremental paging (VERIFIED). One
   call per tracked token per poll.
2. `getTransaction(signature, {maxSupportedTransactionVersion: 0})` per new
   signature.

Per-token cost per poll = 1 + (new swaps since last poll). The rate is therefore
governed by **how many tokens we track and how fast they trade**, both of which
we choose, not by mainnet throughput.

Working the budget conservatively — target 25% of the documented single-method
cap, i.e. **1 req/s sustained**, to leave headroom for the "subject to change
without notice" clause and for 429 backoff:

- ~86,400 requests/day.
- Reserve ~20% for signature paging → ~69,000 `getTransaction`/day.
- At **~2,000–5,000 usable swap records/day** after filtering failures,
  non-swaps, and unusable venues (**INFERRED**, dominated by an unmeasured
  swaps-per-signature yield), a corpus of 100k+ realized fills accumulates in
  **roughly three to seven weeks**.

That is a real corpus, on free infrastructure, and it is far larger than any
volume of self-trading could produce — which is the proposal's central claim and
it survives.

### 7.4 Operational cautions that are not optional

- **429 and 403 are expected outcomes, not errors to retry blindly.** A 403 is
  "traffic is blocked", and hammering through it is how a shared endpoint gets
  an IP banned. Exponential backoff with a hard daily cap, and a `403` treated
  as a **stop condition for the pass**, not a retry.
- **Requests-per-method, not requests-total, is the real cap.** A design that
  spends its whole budget on `getTransaction` hits 4 req/s, not 10.
- **The concurrency cap is 40 connections and the connection *rate* cap is 40
  per 10 s.** A naive client opening a fresh connection per request will hit the
  connection-rate cap long before the request cap. Connection reuse is required.
- **Rate limits "may change without prior notice"** (VERIFIED). Any capacity
  claim built on these numbers must be re-measured, not inherited — the same
  discipline `CRYPTO-QUERY-PLAN-AND-DENOMINATOR-RECOVERY-001` learned when 28
  reviewer trials failed to predict a 45-second block in production.
- All of the above is a **documentation** claim. No request was made. §13.

---

## 8. Link 6 — is MEV / sandwich activity detectable read-only?

**Answer: YES at the population level, NO for our own counterfactual. That
distinction is the whole content of this section, and it is what lets
`SOLANA-ROUTE-OBSERVATION-001` §8.1 row 8 be narrowed rather than upheld.**

### 8.1 Why it is detectable, mechanically

A sandwich is three transactions in one block on one pool: attacker buys,
victim's swap executes at the worsened price, attacker sells. Everything needed
to identify that pattern is in data §3–§5 already established:

- **Same-block grouping is free.** `getSignaturesForAddress` returns a `slot`
  per signature (VERIFIED). Group by slot; any pool with ≥2 signatures in one
  slot is a candidate. **This means sandwich screening costs nothing extra to
  detect — only the candidates need their full transactions fetched**, which
  keeps it inside §7's budget.
- **Ordering does not have to be trusted.** Rather than relying on the order of
  `getBlock`'s `transactions` array (**INFERRED** to be execution order; not
  stated in the docs I read), the order can be **reconstructed from the balance
  chain**: each transaction's `preTokenBalances` on the shared vault must equal
  the previous one's `postTokenBalances`. That chain is a total order, it is
  self-verifying, and a break in it is a detected gap rather than a silent
  mis-ordering. (INFERRED, from the VERIFIED per-transaction capture semantics
  in §5.2.)
- **The attacker signature is behavioural, not identity-based.** Across the
  bracketing pair: net token position ≈ 0, net SOL positive, same signer or same
  token accounts, same pool, same slot. No labelling service, no address
  reputation list, no paid feed.
- **The victim's excess cost is computable.** You have the victim's realized
  price and, from the *front-run's* `preTokenBalances`, the pool state that
  would have priced the victim's swap had the front-run not occurred. The
  counterfactual clean price is arithmetic on a constant-product curve.

### 8.2 Why this is more valuable than it first appears

The proposal describes MEV detection as answering "the dominant residual in AMM
execution". It is more than that — **it is a correctness requirement for the
corpus, not an enrichment of it.**

A calibration corpus that silently mixes sandwiched and clean fills produces a
slippage model inflated by the sandwich rate times the average extraction, with
**no way to decompose the two after the fact**. The fitted model would then be
systematically pessimistic in a way that looks like honest conservatism and is
actually an artifact. Being able to tag each row `clean` / `sandwiched` /
`same_block_contended` turns that from a hidden bias into two separate,
separately-reportable regimes.

It also supplies something no quote endpoint can: an **empirical distribution of
extraction magnitude by trade size**, measured on other people's money.

### 8.3 What is still NOT observable, stated precisely

- **Our own extraction.** MEV is a response to a specific order's visibility to
  searchers and to the leader. We have no order, so there is no counterfactual
  to observe. `SOLANA-ROUTE-OBSERVATION-001` §8.2 is right that this "does not
  exist until you send it", and this document does not disturb that.
- **Whether the population rate transfers to us.** Observed sandwich rates are
  conditioned on *other traders'* slippage tolerances, priority fees, RPC
  routing, and whether they used a private relay — none of which we would share.
  A population rate is a **prior**, not a prediction, and any use of it in a
  modeled fill is a MODELED input requiring the `PAPER_SIMULATION` model
  identifier and modeled-vs-observed basis.
- **Private-orderflow sandwiches.** Extraction routed through private channels
  may not appear as adjacent same-block public transactions at all, so the
  observed rate is a **lower bound**. Reporting it as *the* rate would be the
  fabrication shape this repository names elsewhere as truncation presented as
  completeness.

### 8.4 The precise narrowing this implies

`SOLANA-ROUTE-OBSERVATION-001` §8.1 row 8 currently reads:

> | 8 | MEV / sandwich extraction | **NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION** |

That is true of *our* extraction and false of *the population's*. The row should
split into 8a (population extraction rate and magnitude — **OBSERVABLE
read-only, as a lower bound**) and 8b (our own extraction — **NOT OBSERVABLE**,
unchanged). See §12.

## 9. Selection bias — you observe the sizes traders chose

This is the single largest methodological weakness of the whole approach, it is
**not** fixed by collecting more data, and it deserves more than a caveat.

### 9.1 The identification problem, named

In a designed experiment the input `x` is chosen independently of the response.
`SOLANA-ROUTE-OBSERVATION-001` §4 is exactly that: a frozen ladder of four
notionals applied to every token regardless of what the token looks like.

An observational swap corpus is the opposite. Traders **see the pool before they
size**, so size is chosen *as a function of* the state you are trying to
condition on. This is the classical simultaneity problem — the same reason a
demand curve cannot be estimated from a scatter of equilibrium prices and
quantities — and it means a naive regression of realized impact on size is
estimating a mixture of the AMM's cost function and the traders' sizing policy.

### 9.2 Quantified against this project's own ladder

The concrete damage is a **collapse of support in the variable that matters**.
The relevant covariate is not size but `size / TVL`, since that is what a
constant-product curve responds to.

- **Designed ladder (SRO-001 §4.1, V2):** $10 / $50 / $150 / $500 against a
  measured median pool of $2,860 → `size/TVL` spans **0.35% to 17%**, a **~50x
  range**, chosen deliberately to be non-degenerate.
- **Observational corpus, if traders target a slippage budget:** a trader
  tolerating 0.5–2% impact on a constant-product pool is choosing
  `size/TVL ≈ 0.005–0.02` → a **~4x range**.

So the corpus would carry **roughly an order of magnitude less variation in the
covariate than the designed ladder**, while carrying orders of magnitude more
rows. **Rows are not information here.** And the loss is concentrated in exactly
the worst place: **there would be almost no observations near the top rung**,
because nobody voluntarily accepts 17% impact — which is precisely where a fill
model is least certain and where a bad extrapolation is most expensive.

Four more selection effects, each with its sign:

| effect | mechanism | direction |
|---|---|---|
| **landed-only** | you see fills that executed; swaps that aborted on a slippage limit are absent | biases realized slippage **down** — the worst outcomes are censored |
| **trader composition** | early memecoin flow is dominated by bots and snipers with atypical size, latency and fee behaviour | fits a model of **bot** execution, not of a discretionary entry |
| **life-stage clustering** | memecoin volume is violently front-loaded, and SRO-001 §4.2.1 measures median liquidity falling **$13,586 → $2,860 (4.75x)** from birth to observation | the corpus's pool-state distribution is **not** the distribution at the 6h/24h horizons this project actually cares about |
| **inherited enrolment ceiling** | scoping collection to the sparse lane's tokens inherits its 41.4% birth-eligibility ceiling (SRO-001 §4.4.1) | every rate is conditional on 41.4%, and must be stated against that denominator |

Two of these are partially rescuable, and it is worth being precise about which:

- **The landed-only bias is measurable, not merely acknowledged.** Failed
  transactions are returned by `getTransaction` with a non-null `err` and a
  charged `fee` (VERIFIED, §3.3c). So the **abort rate** is directly observable
  even though the counterfactual price is not. That converts an invisible
  censoring into a quantified one — a strictly better position than the quote
  lane, which cannot see aborts at all.
- **The memecoin caveat cuts the other way on the tail.** Memecoin traders are
  frequently *not* slippage-disciplined (tolerances of 10–50% are routine), and
  panic exits are size-insensitive. The 4x figure above may therefore be
  pessimistic. **This is an empirical question, and it is cheap to settle**:
  §13-C7 is the check.

### 9.3 The reframe that makes the bias much less damaging

The strongest response is not a statistical correction but a change in what the
corpus is asked to do.

**We are not fitting `impact = f(size, state)` from scratch.** For a
constant-product pool `f` is *known analytically* — `x·y = k` gives the exact
output for any input, and §5 recovers `x` and `y` exactly. The same is true of
pump.fun's curve given §5.4. The corpus's job is therefore to measure the
**residual** between the analytic prediction and what actually happened:

```
residual = realized_price − analytic_price(pre_state, amount_in)
```

That residual is where the interesting, unmodellable content lives — fee tiers,
Token-2022 transfer fees, routing across multiple pools, same-block contention,
and sandwich extraction — and there is good reason to expect it to be far **less
size-dependent** than the impact itself. **Endogeneity in `size` damages
structure estimation badly and residual estimation much less**, because the
residual's leading terms are proportional or additive rather than curvature.

Consequences that should be built in from the start:

- Report **per-stratum n by `size/TVL` decile**, always. A stratum with n<30 is
  reported as too thin, in the same spirit as this repository's existing
  `too_thin_to_calibrate` labels.
- Declare an explicit **extrapolation boundary** — the observed support of
  `size/TVL` — and refuse to emit a modeled number outside it, rather than
  emitting one with a wide interval. This repository's EDGE-SELECTION-001
  experience is directly on point: candidates that looked good in-sample
  inverted out-of-sample, and the negative control beat them.
- Treat the corpus as **calibrating and falsifying a known model**, never as
  discovering one.

---

## 10. VERDICT

> **YES — read-only realized-execution ground truth IS obtainable on Solana,
> for free, without ever constructing, simulating, signing, or submitting a
> transaction. All six links hold. The crux (L3) holds more strongly than the
> proposal assumed: pre-trade pool state does not require a prior-slot lookup at
> all, because the AMM's own vault balances are inside the swap transaction's
> `preTokenBalances`.**

The evidence chain, with its weakest joint named:

| link | verdict | strength |
|---|---|---|
| L1 — `getTransaction` returns fee + pre/post SOL and token balances | **HOLDS** | **VERIFIED** in the official RPC reference, field by field |
| L2 — swaps identifiable without a paid decoder | **HOLDS** | **VERIFIED + INFERRED** — the detector is a balance-delta statement needing no IDL and no allowlist |
| L3 — pre-trade pool state recoverable | **HOLDS for constant-product and pump.fun; PARTIAL for CLMM/DLMM** | **VERIFIED** in Agave's `collect_token_balances`, which iterates every account key |
| L4 — availability | **HOLDS PROSPECTIVELY ONLY** | **VERIFIED** architecture; retrospective backfill is **NOT OBTAINABLE** (paid archival) |
| L5 — rate limits permit a useful rate | **HOLDS FOR TARGETED COLLECTION** | **VERIFIED** limits; the firehose fails on the 100 MB/30 s data cap |
| L6 — MEV detectable | **HOLDS at population level; NOT for our own extraction** | **INFERRED** from VERIFIED per-transaction capture semantics |

**The weakest joint is not any of the six. It is §9** — the corpus records the
sizes traders chose, not a designed ladder, and that costs roughly an order of
magnitude of covariate support in exactly the region where a fill model is least
certain. The mitigation is the §9.3 reframe: use the corpus to measure the
**residual** of an analytically known curve, not to discover the curve.

### 10.1 What this does and does not dissolve

**Dissolved:** *"validating a fill model requires realized slippage, which
requires a paid per-trade feed."* The premise is false. Realized execution of
third parties is public consensus data, free, and richer than a trade feed
(which typically reports price and size but **not** the pre-trade pool state
that makes them meaningful).

**Not dissolved, and unchanged:**

- **Landing probability and slot delay** (SRO-001 §8.1 row 7). Unobservable
  without submitting. Untouched.
- **Our own MEV extraction** (row 8b). Untouched.
- **`PaperFill` remains a MODELED artifact.** SRO-001 §8.3's finding — *"Any
  `PaperFill` this project ever writes is a MODEL OUTPUT, never a
  measurement"* — **stands, in full.** Nothing here makes a fill observed.

What changes is the *quality of the model's basis*, and it changes materially:
the slippage component of a modeled fill moves from **ASSUMED** to
**CALIBRATED AGAINST OBSERVED THIRD-PARTY EXECUTION**. Under
`PAPER_SIMULATION`, every artifact must carry a modeled-vs-observed basis; today
that basis would have to say "slippage: MODELED, assumed". With this corpus it
can say "slippage: MODELED, calibrated against N observed realized fills in
stratum S". **That is the difference between a boundary field that is honest but
vacuous and one that carries information.**

### 10.2 The bonus nobody asked for — it makes SRO-001's own verdict falsifiable

`SOLANA-ROUTE-OBSERVATION-001` SC-5 requires the report to reach one of
`execution_quote_trustworthy` / `…_with_stated_gaps` / `…_not_trustworthy`, and
§1 of that document defines the objective as deciding "**whether a trustworthy
`ExecutionQuote` … can be built**".

As scoped, that milestone has **no way to test trustworthiness** — only field
completeness, digest reproducibility, and failure rates. It can establish that a
quote was recorded faithfully; it cannot establish that the quote was *right*.

A realized-execution corpus supplies the missing comparator: **did the venue's
reported `price_impact` for size X on pool P match what actually happened to
real traders at comparable size on P in the same minutes?** That is a direct,
free, read-only falsification test of the quote endpoint, and it turns SC-5 from
a completeness audit into a genuine validation. **This is arguably a larger
result for that milestone than the calibration corpus itself.**

## 11. Prospective collection design (only if the verdict is YES)

*(pending)*

## 12. What this means for SOLANA-ROUTE-OBSERVATION-001

*(pending)*

## 13. Open questions and human-run checks

*(pending)*

## 14. Sources

*(pending)*
