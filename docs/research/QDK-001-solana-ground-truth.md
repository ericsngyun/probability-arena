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
