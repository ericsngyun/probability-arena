# REALIZED-FILL-CORPUS-001 — measurement contract

**Branch `REALIZED-FILL-CORPUS-001`. Not merged. No capital, no execution.**

This is a measurement contract in the style of
`docs/milestones/KALSHI-TAPE-MEASUREMENT-CONTRACT-001.md`: for every quantity
in the realized-fill record it states **what the quantity is, where it comes
from, what class of claim it is, what its noise floor is, and what would
falsify it.**

---

## 0. VERDICT

```
Canonical fill schema                          COMPLETE
Balance-delta transaction decoder              COMPLETE (6 hard cases, 2 refused)
Fee / priority-fee / tip separation            COMPLETE, cross-checked
Quote -> fill linkage                          COMPLETE (identifier-only, by design)
Markout labeling 1s/5s/30s/5m                  COMPLETE (no price source wired)
eps_fill = C_realized - C_quote_hat            DEFINED, POPULATED 0 ROWS
AS_h = P_{t+h} - P_fill                        DEFINED, POPULATED 0 ROWS
Fixtures from real mainnet transactions        COMPLETE (6 pinned, provenance + drift)
Positive and negative controls                 COMPLETE (decoder proven able to FAIL)
```

**One sentence, if you read nothing else:** *the machinery that records a fill
now exists and is verified against six real mainnet transactions, but the
corpus contains **zero rows of our own** — and it must, because `eps_fill`
requires capital-funded calibration trades that are **not authorized** (§9).*

**What that does and does not unblock.**

| spec | before | after |
|---|---|---|
| `RISK-GOVERNOR-001` §10 | three missing models, `UNCALIBRATED`, only trustworthy output `NO_TRADE` | **unchanged.** The *instrument* exists; the *data* does not. The governor stays `UNCALIBRATED` |
| `ALPHA-FACTORY-001` §5.3 G4 | fee schedule unverifiable against realized fills, gate `UNEVALUATED` | **partly moved.** The Solana fee schedule is now verified against realized fills (§4.3), and one common formulation is **refuted**. The gate itself stays `UNEVALUATED` for lack of *our* fills |
| `VOLATILITY-STATE-ENGINE-001` §3 R4 | `NOT_COMPUTABLE:no_fill_history` | **unchanged**, and the markout labeller now exists to consume the history when it exists. R4 stays `NOT_COMPUTABLE` |

Reporting this as "unblocked" would be the exact failure this repo keeps
paying for. The corpus is an empty instrument, and an empty instrument that
says so is worth more than a full one built from things we assumed.

---

## 1. What already existed, and what this milestone added

**Already existed** (and was reused rather than duplicated):

* `app/adapters/dexscreener.py` — the adapter convention this milestone
  follows: async `httpx`, per-adapter timeout, degrade-to-`None` on transport
  or schema failure, and the CRYPTO-COVERAGE-REPAIR-002 B1 rule that an HTTP
  200 of the wrong shape is a **failed** request, not an empty answer.
* `app/cli.py` — the subcommand + dispatch pattern used for the new verb.
* `app/services/frontier_eval.py` — `BANNED_IDENTIFIER_FRAGMENTS`, the
  identifier-level safety audit this package is written to pass without an
  allowlist entry.
* A read-only Solana **discovery/risk** lane (`crypto_scout`, `crypto_risk`,
  `meme_*`, DexScreener) — token discovery, ticks, deterministic risk scores.

**Did NOT exist anywhere in the repo before this milestone:**

* Any Solana **JSON-RPC** client. The crypto lane talks to DexScreener and
  friends over HTTP; nothing had ever read the chain.
* Any notion of a **fill**, a **markout**, a **realized slippage**, a
  **priority fee**, a **tip**, or a **cost basis**.
* Any **balance-delta** accounting.
* Any **fixture-provenance** mechanism on the Solana side. (Kalshi's exists
  in spirit in `app/realtime/`; that path is frozen and was not touched.)

**Added by this milestone:**

```
app/fills/__init__.py       the written capital boundary
app/fills/absence.py        Observed | Absent with a closed reason set
app/fills/schema.py         the canonical RealizedFill record
app/fills/b58.py            vendored base58 (9 bytes of ComputeBudget operand)
app/fills/fees.py           network / priority / tip separation, cross-checked
app/fills/decoder.py        balance-delta transaction decoder
app/fills/markout.py        1s/5s/30s/5m labeling with an explicit price source
app/fills/linkage.py        quote -> fill linkage, identifier-only
app/fills/calibration.py    eps_fill and AS_h
app/fills/corpus.py         assembly seam + coverage summary
app/fills/provenance.py     two-hash fixture provenance + drift detector
app/adapters/solana_rpc.py  read-only RPC with a closed method allowlist
app/cli.py                  realized-fill-corpus-report (reachability)
scripts/fetch_realized_fill_fixtures.py
tests/fixtures/solana_fills/  6 pinned real mainnet transactions + MANIFEST
tests/test_realized_fill_*.py  the controls
```

---

## 2. Typed absence is the contract's first rule

Doctrine 10, applied to a cost basis where every zero has economic meaning:

| | means |
|---|---|
| `tip = 0` | we inspected the transaction and no tip was paid |
| `tip = unknown` | we could not determine whether one was |
| `markout_5m = 0.0` | the price 5 minutes later equalled the fill price |
| `markout_5m = unknown` | 5 minutes have not elapsed, or we have no price there |
| `eps_fill = 0` | the fill cost exactly what the quote predicted |
| `eps_fill = NOT_AUTHORIZED` | we have never had a quote or a fill of our own |

Absence is structural: `Observed(value, source)` or `Absent(reason)`, and
`Absent` supports **no arithmetic and no truth value** — `bool(Absent)` raises,
so `x or 0` cannot fabricate a zero. The reason set is closed:

`NOT_PROVIDED` · `NOT_APPLICABLE` · `NOT_RECONSTRUCTABLE` ·
`NOT_YET_OBSERVED` · `NOT_AUTHORIZED` · `TRANSACTION_FAILED` ·
`CONFLICTING_SOURCES`

`NOT_AUTHORIZED` exists specifically so `eps_fill`'s emptiness cannot be
mistaken for `NOT_YET_OBSERVED`. **Waiting will not produce it.**

`combine()` propagates absence through every sum. A total cost containing an
unknown term is **unknown**, not the sum of the known ones — which is exactly
`ALPHA-FACTORY-001` §5.3's `VOID_MEASUREMENT` rule rendered in code.

---

## 3. The measurement table

Class key: **VF** = VENUE_FACT (the chain asserted it) · **D** = DERIVED (from
venue facts by a stated rule) · **DL** = DERIVED_LOSSY (the rule provably
loses information) · **NR** = NOT_RECONSTRUCTABLE.

### 3.1 Identity and route

| quantity | source | class | noise floor | what would falsify it |
|---|---|---|---|---|
| `signature` | `transaction.signatures[0]` | **VF** | exact | a payload whose stored signature differs from its pinned provenance (tested) |
| `slot` | `getTransaction.slot` | **VF** | exact | slot disagreeing with a second RPC |
| `t_confirmed` | `blockTime` | **VF** | **±seconds, and not a clock.** `blockTime` is an estimate derived from validator vote timestamps, not a measurement of when the transaction executed | two RPC providers reporting different `blockTime` for one slot |
| `decision_id` / `observation_id` | carried by the caller | **NR** from chain | n/a | — (absent for every third-party fixture, correctly `NOT_APPLICABLE`) |
| `mint`, `side` | **the decision**, not the chain | n/a | n/a | the chain records that assets moved, never which one we considered "the thing we traded" |
| `route.legs` | non-infrastructure program ids invoked | **DL** | a *candidate* list | a route whose venue program routes internally shows as one leg |
| `route.hop_count` | — | **NR** | n/a | **always absent.** Program-invocation count bounds but does not determine hop count |
| `route.pool` | — | **NR** | n/a | **always absent.** Pool identity needs per-program account layouts this decoder deliberately does not encode |

> **Why `hop_count` is absent rather than a number.** Doctrine 8: a field name
> is not evidence of its semantics. `hop_count = 1` on a route we cannot
> topologically resolve would be a confident false finding of exactly the
> `updated_time` shape. **The amounts never depend on the route**, which is
> the point of balance-delta accounting.

### 3.2 Realized amounts — the core

| quantity | source | class | noise floor | what would falsify it |
|---|---|---|---|---|
| `actual_input` / `actual_output` | `meta.pre/postBalances` and `meta.pre/postTokenBalances`, netted per mint for the party | **D** | **exact, in integer base units.** No float ever touches a cost basis | the ledger identity below failing on any confirmed fixture |
| `actual_price` | `actual_output / actual_input`, both decimal-scaled | **D** | exact `Decimal`; **absent if either scale is unknown** — a price built from base units of different decimal scales is off by a power of ten and looks reasonable | — |
| `party_lamport_delta_raw` | `postBalances[i] - preBalances[i]` | **VF** | exact | — |

**The ledger identity, asserted on every confirmed fixture:**

```
party_lamport_delta  ==  output_lamports - network_fee - priority_fee - tip
```

If this fails, some lamport flow is unaccounted for and the cost basis is
wrong by exactly that amount. Measured on
`4FcuJFeixgz…`: `316,041,825 == 316,053,825 − 5,000 − 7,000`. ✅

**Absence semantics that matter here:**

* `meta` missing → **refusal**, never a record of zeroes. A zeroed record
  reads as a trade that moved nothing.
* `preTokenBalances`/`postTokenBalances` missing → `NOT_PROVIDED`. The RPC
  said *nothing* about token movement; it did not say none happened.
* transaction failed → `TRANSACTION_FAILED`, **never zero**. A failed
  transaction costs real money and delivers nothing; recording zero output
  makes it a free trade at a price of zero, and dropping it biases the cost
  basis downward by the failure rate — which is highest in the congested
  regime where signals look strongest.
* more or fewer than one negative and one positive asset delta →
  `NOT_RECONSTRUCTABLE` with the deltas quoted. Not a guess.

### 3.3 Costs — where a wrong cost basis comes from

| quantity | source | class | noise floor | what would falsify it |
|---|---|---|---|---|
| `network_fee_lamports` | `5,000 × numRequiredSignatures` | **D from an ASSUMED constant** | exact **if** the constant holds | `meta.fee` below the floor. Then the code refuses (`CONFLICTING_SOURCES`) and names the falsified constant rather than clamping to zero |
| `priority_fee_lamports` | `meta.fee − base_fee`, cross-checked against `ceil(cu_price × cu_limit / 1e6)` | **D** | **1 lamport** tolerance between the two derivations | any disagreement > 1 lamport → `CONFLICTING_SOURCES`, and the corpus reports the contradiction rather than picking a side |
| `tip_lamports` | **balance delta of registered tip accounts** | **VF** | exact | a tip paid to an account outside the registry. Unregistered outflow from the party is reported as a note so a reviewer sees the candidate |
| `tip_attempted_lamports` | transfer operands to tip accounts | **D** | exact | — |
| `rent_lamports_net` | lamport delta of the party's token accounts, **net of any wrapped-SOL balance inside them** | **D** | exact | a wrapped-SOL account whose lamports are not `rent + wrapped` |
| `compute_units_consumed` | `meta.computeUnitsConsumed` | **VF** | exact | absent on pre-1.15 ledgers → `NOT_PROVIDED` |
| `compute_unit_price` | ComputeBudget `SetComputeUnitPrice`, base58-decoded | **VF** | exact | a price set via a lookup-table-hidden instruction |

**The three terms are separated because merging them corrupts the cost basis,
and the merge is asymmetric:**

| term | who receives it | where it appears | moves with |
|---|---|---|---|
| base network fee | burned / leader split | inside `meta.fee` | signature count only |
| priority fee | leader | inside `meta.fee` | congestion, and our choice |
| tip | a block-engine tip account | **NOT in `meta.fee`** — an ordinary SOL transfer | congestion, and our choice |

A cost model that reads `meta.fee` and stops omits the tip **entirely**, and
the tip is routinely the largest of the three in a competitive block. On
pinned fixture `4VFstbam…` the tip *intent* was 11,300,000 lamports against a
1,005,000 lamport `meta.fee` — an 11× omission had it been paid.

---

## 4. Three measurements this milestone made

### 4.1 A reverted tip is not a paid tip — a defect found in this milestone's own first pass

The first version of the decoder read the tip from the transfer **operand**.
Checked against the ledger:

| fixture | status | tip operand | tip account balance delta | `meta.fee` |
|---|---|---|---|---|
| `4pqbgr92…` | **FAILED** | 1,500,000 | **0** | 605,000 |
| `4VFstbam…` | **FAILED** | 11,300,000 | **0** | 1,005,000 |
| `4FcuJFeixgz…` | ok | 7,000 | **7,000** | 5,000 |
| `SFr9cfiYkEf…` | ok | 1,519 | **1,519** | 5,000 |

A failed transaction reverts every state change **except the fee**. Reading
the operand booked a 1.5M-lamport cost that was never paid **and**
manufactured a positive SOL leg on a transaction that traded nothing. On both
successful transactions operand and delta agree exactly, so the rule is revert
semantics and not noise.

**The tip is therefore a `VENUE_FACT` read from the destination's balance
delta, with the operand retained as `tip_attempted_lamports`.** Doctrine 8
applied to our own field: the operand is *intent*, the delta is *what
happened*, and only one of them belongs in a cost basis.

### 4.2 Naive log parsing is wrong by ~148,000× on a real multi-hop route

Pinned fixture `SFr9cfiYkEf…` is a two-hop cyclic route USDC → WSOL → USDC.

| method | answer |
|---|---|
| last SPL transfer operand ("parse the logs") | **107,232,992** USDC base units |
| largest transfer operand | **1,228,043,049** WSOL base units |
| **balance-delta accounting (this decoder)** | **725** USDC base units |

All three numbers are real; only one is the fill. The intermediate asset nets
to **exactly zero** for the trading party, which is why the delta method
decodes a route it knows nothing about, using the same code path as a direct
one. This is the negative control that justifies the whole approach, and it is
a test, not a claim.

### 4.3 The priority fee is charged on the requested LIMIT, not on units consumed

This is the formulation the milestone brief itself carried, and the fixtures
refute it. On `4VFstbam…`: 4,919 CU consumed, unit price 3,333,333
micro-lamports, requested limit 300,000, and a `meta.fee` residual of exactly
1,000,000 lamports.

```
price × CONSUMED         = ceil(3,333,333 × 4,919   / 1e6) =    16,397
price × REQUESTED LIMIT  = ceil(3,333,333 × 300,000 / 1e6) = 1,000,000   <- matches
```

The `consumed` formulation is low by **61×** on this transaction. It would
understate the priority fee on every over-requested route — which is the
normal case for aggregator routes — and it would understate it in the same
direction as every other optimistic error. The `meta.fee` residual is immune
to this, which is why it is the primary derivation and the budget formula is
only the cross-check.

---

## 5. Hard cases: handled, and NOT handled

| hard case | status | how |
|---|---|---|
| **multi-hop routes** | **HANDLED** | intermediates net to zero for the party; endpoints are exact. `hop_count` stays absent |
| **wrapped SOL** | **HANDLED** | native SOL and WSOL are ONE asset under a single pseudo-mint. Counted once |
| **ATA creation / closure rent** | **HANDLED** | rent = lamport delta of the party's token accounts, **minus** any wrapped-SOL balance inside them, so a wrap is not double-counted. Both an ATA-creating and a non-ATA-creating fixture are pinned, so `rent = 0` is a *measured* zero |
| **failed transactions** | **HANDLED** | output `TRANSACTION_FAILED`, fee real, tip 0, asset deltas empty. Retained in the corpus, not dropped |
| **fee payer is also a trade party** | **HANDLED** | the default. Fee, tip and rent are added back before the SOL leg means anything; the ledger identity is asserted |
| **fee payer is NOT the trade party** | **HANDLED** | fee is reported but not deducted, and the record says so. Exercised on a real counterparty in a real transaction |
| **v0 / address-lookup-table account ordering** | **HANDLED** | both `json` and `jsonParsed` orderings; a shifted account list provably changes the answer |
| **PARTIAL FILLS** | **PARTIALLY — logic only, NO REAL FIXTURE** | detected structurally as `actual_input < quoted_input`. **A partial fill is defined against a QUOTE, and a third-party transaction carries no quote we can see**, so the branch is exercised by a constructed linkage test rather than by pinned venue evidence. `tests/test_realized_fill_fixtures.py` asserts `partial_fill` is *absent* from the fixture set so the silence cannot rot into an assumed coverage claim |
| **pool identity per leg** | **NOT HANDLED** | needs per-program account layouts. Typed `NOT_RECONSTRUCTABLE`, never guessed |
| **token-2022 transfer fees / hooks** | **NOT HANDLED** | a transfer-fee extension takes a cut that appears in the balance delta but is not separable from price impact by this decoder. No pinned fixture exercises it. **This is a live way to misattribute cost — see §12** |
| **token balances with no `owner`** | **NOT HANDLED** | pre-owner-field RPC responses cannot be attributed to a party; counted and reported as a note |
| **a route where the party holds several accounts of one mint** | HANDLED by summation, **UNVERIFIED** — no fixture exercises it |

---

## 6. Markouts, and why the price source is part of the measurement

Horizons are fixed at **1s / 5s / 30s / 5m**, declared before any data is
seen (`ALPHA-FACTORY-001` §7.1).

| price source | what it measures |
|---|---|
| `SAME_POOL_TRADE` | a trade that actually occurred in the pool we traded |
| `SAME_POOL_RESERVES` | the pool's own reserves at the markout slot |
| `OTHER_VENUE` | a different venue's price for the same pair |
| `AGGREGATOR_SNAPSHOT` | a pair-level API snapshot |
| `INTERPOLATED` | a value between two observations bracketing the horizon |
| `NONE_AVAILABLE` | no price |

**A markout computed against a different venue is not the same quantity as one
computed against the pool we traded.** The difference is a venue basis, and a
venue basis is *persistent and signed* — exactly the shape R4 toxicity
detection is looking for. Feed R4 cross-venue markouts and it reports adverse
selection that is really an arbitrage spread nobody was crossing.

So every `Markout` carries its source, no consumer may drop it, and
`worst_price_source()` exists so an aggregate cannot inherit the quality of its
best member (doctrine 10's inheritance rule).

**Interpolation is opt-in and downgrades the source.** In a jump regime — the
regime that matters — interpolation removes exactly the excursion the markout
exists to catch, and it does so in the direction that flatters us.

**Tolerance.** An observation more than `0.5 × horizon` away (floor 250 ms) is
**refused**, not relabelled: a "1s markout" taken 41s late is a 41s markout
wearing the wrong label, and mislabelling is worse than losing it because it
survives into a statistic. The disqualifying offset is still recorded.

> **LOUD PLACEHOLDER.** The 250 ms floor is asserted from block cadence and is
> **NOT measured** on any collector of ours. Falsified by: measuring our own
> observation timestamp jitter and finding it exceeds 250 ms.

| quantity | source | class | noise floor | what would falsify it |
|---|---|---|---|---|
| `markout_{1s,5s,30s,5m}` | a `PriceObservation` within tolerance | **D**, inheriting its source's class | **bounded by `observation_offset_ms`, which is always recorded** | an observation whose `PriceSource` is not the pool traded, presented as if it were |
| `observation_offset_ms` | observation time − horizon | **D** | exact | — |

**Today all four horizons are `NOT_PROVIDED` on all six fixtures.** No price
source is wired: doing so would mean either paying for one (forbidden by
doctrine 17 at this tier) or reconstructing pool reserves per program, which
§5 says we cannot do. That absence is reported per horizon with its reason.

---

## 7. The two calibration quantities

### 7.1 `eps_fill = C_realized − C_quote_hat`

All-in cost as a fraction of notional, against a declared benchmark price
`P_bench` in one orientation for both terms:

```
C(P_exec) = direction_sign · (P_exec − P_bench) / P_bench
          + (network_fee + priority_fee + tip) / notional_quote_units
```

* `direction_sign` = `+1` for `ACQUIRE`, `−1` for `DISPOSE`. It lives in one
  function because it is the single easiest thing in this document to invert.
* `C_quote_hat` uses `P_exec = quoted_price` and the **modelled** lamport
  terms declared *before* submission.
* `C_realized` uses `P_exec = actual_price` and the **observed** lamport terms.
* `P_bench` must be identical for both, or the subtraction is meaningless.
  Default: the quoted price. With that default `C_quote_hat`'s price term is
  exactly zero and `eps_fill`'s price term is exactly the realized slippage —
  which is what "residual against the quote" means.

**Positive `eps_fill` means the fill cost more than the quote predicted**, the
direction that turns a real edge into a false graduate. `ALPHA-FACTORY-001`
sets the scale: E2 was a genuine lead at **70% of its cost floor**, so a cost
model 30% optimistic converts our one real historical finding into a graduate.

**A non-SOL notional refuses the lamport term.** Lamport costs are in SOL;
dividing them by a USDC notional without a SOL/USDC rate is precisely how a
cost basis ends up wrong by the SOL price. The honest output is
`NOT_RECONSTRUCTABLE`.

### 7.2 `AS_h = P_{t+h} − P_fill`

Both prices in **quote units per unit of base asset**. `P_fill` comes from
`actual_price` through one named inversion (`fill_price_quote_per_base`),
because that inversion is where a markout sign silently flips.

`AS_h` is unsigned by direction — it is a statement about the market.
`adverse_selection_signed` applies the direction and answers the question a
risk system asks: **negative is adverse.** R4 needs the signed quantity; fed
the unsigned one, a toxicity detector fires on direction instead of toxicity.

| quantity | source | class | noise floor | what would falsify it |
|---|---|---|---|---|
| `eps_fill` | `C_realized − C_quote_hat` | **D** | dominated by the markout/benchmark price noise, not by the fee terms, which are exact | a quote reconstructed after the fill — that is a fit, not a prediction |
| `AS_h` | `markout.price − P_fill` | **D**, inheriting the markout's `PriceSource` | **the price source's own basis.** A cross-venue `AS_h` cannot resolve an effect smaller than the venue basis | computing it from a markout whose price is `Absent` (impossible: it propagates absence) |

**Both are populated on 0 rows.** See §9.

---

## 8. Fixture provenance and drift

Six pinned real mainnet transactions, retrieved read-only over the **free
public** endpoint `https://api.mainnet-beta.solana.com` (doctrine 17, tier 0 —
no paid RPC, no managed stream, no MEV-class access).

| capture id | hard cases |
|---|---|
| `direct_dispose_wrapped_sol_ata_cycle` | wrapped SOL, ATA create+close rent, fee payer is party, v0 + lookup table, tip |
| `direct_dispose_no_ata_creation` | wrapped SOL, **measured zero rent**, v0, tip |
| `multi_hop_cyclic_route` | multi-hop, **naive log parse wrong**, wrapped SOL, v0, tip |
| `multi_hop_counterparty_view` | **fee payer is NOT the party** (same transaction, counterparty perspective) |
| `failed_transaction_legacy_high_priority` | failed, priority fee 600,000, **reverted tip intent**, legacy |
| `failed_transaction_v0_high_priority` | failed, priority fee 1,000,000, reverted tip, **limit-not-consumed proof**, v0 |

Every entry records: `capture_id` · `venue` · `chain_genesis_hash` ·
`signature` · `slot` · `block_time` · `rpc_endpoint` · `rpc_method` ·
`rpc_encoding` · `rpc_commitment` · `rpc_max_supported_version` ·
`retrieved_at` · `retrieved_by` · `schema_version` · `content_sha256` ·
`semantic_sha256` · `hard_cases` · `selection_reason`.

**Two hashes, because they detect different things.** A finalized transaction
is immutable, so unlike a Kalshi WS frame the *content* cannot drift. What
drifts is the RPC's *representation* — encodings, field presence, `jsonParsed`
coverage as programs gain parsers, `maxSupportedTransactionVersion` behaviour.
That is exactly what would change our decoder's input while every test stayed
green.

* `content_sha256` over the stored bytes → detects local mutation, offline,
  every run.
* `semantic_sha256` over a canonicalized subset containing **exactly the
  fields the decoder reads** → compared against a live re-fetch by the gated
  drift test.

The drift detector is proven able to **fire** (mutate `meta.fee` → drift, with
`meta.fee` named) and proven to **stay quiet** on a field the decoder never
reads (a new advisory field is not venue drift). A failed live fetch is
reported as **UNKNOWN drift, not absent drift**.

**Selection bias, stated.** All six were found by walking
`getSignaturesForAddress` over public block-engine tip accounts. That is a
**tip-paying, latency-sensitive population** — disproportionately arbitrage
and sniping bots. It is the right population for exercising tips, priority
fees and multi-hop routes, and the **wrong** population from which to infer
anything about typical route economics. Nothing in this milestone infers
anything statistical from them; they are decoder ground truth only.

---

## 9. THE CAPITAL BOUNDARY — explicit and binding

**`eps_fill` cannot be populated with real data by anything in this
repository, and this milestone does not change that.**

`eps_fill = C_realized − C_quote_hat` needs two things we do not have and
cannot obtain here:

1. **`C_quote_hat`** — a quote recorded *before* submission. Fetching a quote
   is `READ_ONLY_ROUTE_QUOTE`, which `docs/SAFETY_BOUNDARIES.md` /
   SAFETY-BOUNDARY-ROUTE-QUOTE-001 permits **only under a separately accepted
   milestone that does not exist**. There is no authorization to build a quote
   fetcher today.
2. **`C_realized` for a fill of ours** — an executed trade. That requires
   capital, key material, transaction construction, signing and submission.
   **All are hard-forbidden**, and `RISK-GOVERNOR-001` §11 is explicit that a
   fill corpus only begins to exist at the **TINY CAPITAL** rung, whose stated
   purpose *"is not profit — it is to create the fill corpus §10 requires."*

**No code path in this milestone can execute a trade, and this is structural,
not a promise:**

* `app/adapters/solana_rpc.py` enforces a **closed allowlist** of read-only
  methods. `sendTransaction`, `simulateTransaction`, `getLatestBlockhash`,
  `getRecentBlockhash`, `getFeeForMessage`, `getRecentPrioritizationFees` and
  `requestAirdrop` are refused **before a socket is opened**, and the refusal
  is a hard exception, not a degradation. Tested.
* **No module in `app/fills/` may import a networking client at all** —
  `httpx`, `requests`, `aiohttp`, `socket`, `websockets`, `urllib` are all
  forbidden there and the ban is enforced by an AST test. The decoder, the
  calibration maths and the corpus assembly are pure functions over JSON.
* No key material, no seed phrase, no signer, no transaction encoder, no
  instruction builder exists anywhere in the package.
* No identifier in the package contains any fragment from
  `BANNED_IDENTIFIER_FRAGMENTS`; **no allowlist entry is requested**.

**What a future authorization would have to specify** — a milestone that
merely says "allow calibration trades" is not enough:

1. **The capital ceiling**, in absolute SOL, and the per-trade maximum loss,
   sized so that being *completely wrong* is affordable rather than so that
   being *right* is meaningful (`RISK-GOVERNOR-001` §11).
2. **The number of trades and the stopping rule**, preregistered.
   `QUANT-DECISION-KERNEL-001` puts detecting a 1pp edge at **30,000–75,000**
   prospective trades; a calibration corpus targets `eps_fill`'s *distribution*
   rather than an edge, so it needs its own preregistered power calculation and
   must say which one it is doing.
3. **Who holds the key material, where, and under what custody** — this
   repository must not be the answer.
4. **The mechanical kill switch**: which counter, at which threshold, latches
   and stops submission, and who can unlatch it (`RISK-GOVERNOR-001` §12 —
   latching, not self-clearing).
5. **The quote-fetch authorization separately**, because
   `READ_ONLY_ROUTE_QUOTE` is its own permitted-with-conditions mode and
   reaching a quote route grants nothing on any other route.
6. **The exact set of RPC methods** to be added to
   `PERMITTED_METHODS`, one at a time, each in its own reviewable change.
7. **The preregistered `C_quote_hat` model** — the modelled lamport terms must
   be declared *before* the first submission. A cost model fitted after the
   fills are known is not a prediction, and the residual against it is not a
   residual.
8. **The abort condition on `eps_fill` itself**: at what observed residual the
   programme stops rather than continues to buy data.

Until every one of those exists, the honest value of `eps_fill` is
`NOT_AUTHORIZED`, and the corpus says so on every row.

---

## 10. Reachability

Doctrine 5: a checkpoint is complete when its intended production path is
demonstrably reachable, asserted from **outside**.

```
python -m app.cli realized-fill-corpus-report [--format json]
```

Offline, read-only, opens no socket. Decodes the pinned fixtures through the
real decoder and prints per-field coverage **with absence reasons**. Exit 1 on
any provenance violation — proven by a test that corrupts a manifest hash and
requires the non-zero exit.

Measured output:

```
fixtures           : 6
provenance problems: 0
status counts      : {'confirmed': 4, 'failed': 2, 'undecodable': 0}
actual_output        4/2 {'transaction_failed': 2}
network_fee          6/0
priority_fee         6/0
tip                  6/0
rent                 6/0
realized_slippage    0/6 {'not_reconstructable': 6}
markout_1s..300s     0/6 {'not_provided': 6}
eps_fill rows      : 0   (NOT_AUTHORIZED, not not-yet-collected)
```

The seam tests stay in the suite permanently. They are the guard, not
scaffolding.

---

## 11. Validation

Run on `python -m pytest`, actual counts:

| module | result |
|---|---|
| `tests/test_realized_fill_decoder.py` | **17 passed** |
| `tests/test_realized_fill_fees.py` | **14 passed** |
| `tests/test_realized_fill_calibration.py` | **28 passed** |
| `tests/test_realized_fill_fixtures.py` | **13 passed, 1 skipped** (gated live drift) |
| `tests/test_realized_fill_seam.py` | **10 passed** |
| **milestone total** | **82 passed, 1 skipped** |

**The decoder is proven able to FAIL.** Negative controls, each forcing the
condition and requiring the metric to become non-benign:

* one lamport changed in `postBalances` → output moves by exactly one lamport
* a token pre-balance changed by 1,000 → input moves by exactly 1,000
* `meta.fee` corrupted → the **second** priority-fee derivation catches it and
  the fee becomes `CONFLICTING_SOURCES`, not the convenient number
* one account inserted at the front of the key list → the answer changes
* `meta` removed → refusal, not a record of zeroes
* token-balance arrays removed → `NOT_PROVIDED`, not zero
* a failed transaction → output absent, fee real, tip 0 despite a 1.5M operand
* a multi-hop route → naive log parsing differs by >100,000×

A decoder that returned the same answer under any of these would be reading
something other than the ledger it claims to measure.

---

## 12. The single most likely way this corpus produces a wrong cost basis

**A cost term that exists on chain, lands inside the balance delta, and is
therefore silently absorbed into `actual_output` as if it were price impact.**

The decoder's strength is that it measures the *net* position change and
therefore cannot miss value that moved. That is also its blind spot: it cannot
tell *why* value moved. Every one of these is invisible to it and each biases
the same direction — inflating apparent price impact and deflating apparent
fees:

* a **Token-2022 transfer-fee extension** taking a cut of the output token;
* an **aggregator or router platform fee** taken in-kind from the output leg;
* a **referral / integrator fee** routed to a third account inside the route;
* a **tip paid to an account outside `KNOWN_TIP_ACCOUNTS`**, which looks like
  an ordinary transfer and is currently only *reported as unattributed*, not
  attributed.

The consequence is not that the total is wrong — the all-in number stays
right — but that the **decomposition** is wrong, and the decomposition is
exactly what `ALPHA-FACTORY-001` §5.3 gates on. Its measured lesson was that
the binding constraint was the **fee minimum**, which *does not improve with
better execution*, while price impact does. A corpus that misfiles a fixed
in-kind fee as price impact will report a cost that looks reducible by better
execution and is not — and it will do so most on small routes, where a fixed
fee is the largest fraction of notional, which is precisely the size a
calibration programme would trade.

The second-most-likely: **the markout price source**. All four horizons are
currently absent, which is safe. The moment a cross-venue or aggregator price
is wired in without the source travelling with the number, `AS_h` will report
a persistent venue basis as adverse selection, and R4 will fire on an
arbitrage spread nobody was crossing. The typed `PriceSource` and
`worst_price_source()` exist to make that a visible choice rather than an
accident, but they cannot prevent a consumer from ignoring them.

---

## 13. What this milestone cannot do

* It cannot make `RISK-GOVERNOR-001` calibrated. The governor stays
  `UNCALIBRATED` and its only trustworthy output stays `NO_TRADE`.
* It cannot make R4 computable. `NOT_COMPUTABLE:no_fill_history` stands.
* It cannot pass `ALPHA-FACTORY-001` G4. It verifies the *Solana* fee schedule
  against realized fills — including refuting one common formulation — but the
  gate needs *our* fills.
* It cannot resolve pool identity, per-leg mints, or hop topology.
* It cannot separate an in-kind protocol fee from price impact (§12).
* It cannot observe `t_submit` for any transaction that is not ours, so
  `quote_to_submit_ms` is structurally unavailable for every current row.

### Falsification

This contract is wrong if any of the following is observed:

1. The ledger identity `party_delta == output − fee − priority − tip` fails on
   a confirmed transaction where all terms are `Observed`.
2. `meta.fee` on mainnet falls below `5,000 × numRequiredSignatures`.
3. The two priority-fee derivations disagree by more than 1 lamport on a
   transaction with a readable compute budget.
4. A transaction is found where the tip account's balance delta and the
   transfer operand disagree **while the transaction succeeded**.
5. A live re-fetch of any pinned signature produces a different
   `semantic_sha256` — the RPC's representation of an immutable transaction
   has moved, and our decoder's input surface moved with it.
