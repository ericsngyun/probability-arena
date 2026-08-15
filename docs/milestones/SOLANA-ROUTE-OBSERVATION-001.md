# SOLANA-ROUTE-OBSERVATION-001 — route observation, executable-quote decision

**Status: PLAN ONLY — ACCEPTED, NOT BUILT.** The milestone is accepted by Eric
(§0.1). This document is its design and its preregistration. No production code,
no feature flag, no migration, no schema change, no behaviour change, no
deployment, and **no provider call** has been made for it. Nothing here is
implemented.

Branch base: `main` @ `2a5f701` (`app/canon.py` and `AGENTS.md` reconciled to the
route-quote amendment). Predecessor: `docs/SOLANA_ROUTE_OBSERVATION_001_SCOPE.md`
on branch `SOLANA-ROUTE-OBSERVATION-001-scope` @ `f894e82`, which is based on the
stale `8790a25` and **must not be merged** — its diff deletes work that has since
landed. This document supersedes it; only the doc content was carried forward.

**EVO-X2 was not contacted for this document.** The host is now reachable and
the sparse observation lane is live, but this agent made no measurement on it.
Every number below is either read out of this repository (cited by file and
line) or explicitly labelled **PENDING MEASUREMENT** with the exact query that
would settle it (§14). Nothing is asserted as measured that was not.

---

## 0. Acceptance, and what changed since the scope doc

### 0.1 The authorizing instruction, verbatim

Eric, 2026-08-14 (item B4):

> B4 — Start SOLANA-ROUTE-OBSERVATION-001. Once 6h mechanics are demonstrably
> healthy and 24h jobs are being scheduled correctly, begin this milestone
> immediately while the first 24h observations mature. The capability is
> READ-ONLY.
> Allowed: quote request · executable route response · route/pool analysis ·
> price impact observation · fee observation
> Forbidden: wallet · signing · swap instruction generation · serialized
> transaction generation · simulation that requires transaction construction ·
> submission/broadcast · capital
> Capture for fixed predeclared paper notionals: observed_at · token_in ·
> token_out · input_amount · amount_out · minimum/threshold output where
> provided · executable price equivalent · price impact · fees · route ·
> DEX/pools · route split · context slot · request latency · evidence digest
> Use fixed notionals declared BEFORE evaluating results. Do not optimize
> notionals after seeing favorable execution.

Two conditions in that instruction are **gates on starting, not decoration**:
"once 6h mechanics are demonstrably healthy" and "24h jobs are being scheduled
correctly". Neither was verified by this agent. They are CP-0 entry conditions
(§9) and are PENDING MEASUREMENT (§14, M1/M2).

### 0.2 What the scope doc got right and this document keeps

Carried forward substantially unchanged: the observable/estimated/impossible
split (§8), the finding that landing probability and adversarial extraction
cannot be observed without submitting a transaction, the fabrication-shape
catalogue, the "an estimate is never an observation" invariant, the reuse of the
sparse lane's exact-token identity gate, and the refusal to re-inflate
`raw_payload`.

### 0.3 What the scope doc got wrong, and is corrected here

| scope doc said | now |
|---|---|
| **Q1 — may we call an aggregator quote endpoint? "TIER 3. I will not make this call."** | **ANSWERED YES, conditionally.** `docs/SAFETY_BOUNDARIES.md` amendment SAFETY-BOUNDARY-ROUTE-QUOTE-001 (2026-08-14) permits capability mode `READ_ONLY_ROUTE_QUOTE`. The conditions are strict and enumerated in §3. |
| **Q4 — paper simulation is forbidden, so route observation has no permitted consumer.** | **ANSWERED, conditionally.** The same amendment permits `PAPER_SIMULATION`: **modeled** fills and **modeled** P&L only, each artifact carrying an explicit model identifier and a modeled-vs-observed basis. MVP-005B still governs whether such a lane is BUILT. This milestone still builds **no** fill (§11). |
| The whole design assumed **no** quote endpoint: route composition was a *proxy* from the DexScreener pool inventory, and price impact was a *declared model* over provider TVL. | With `READ_ONLY_ROUTE_QUOTE` permitted, route composition, output amount, price impact and fee become **OBSERVED from the quote response** rather than modeled. The proxy design is demoted to a fallback (§8.4) used only if quoting turns out to be unavailable or not free. |
| Data model centred on `est_impact_bps_at_notional_*` — a modeled number. | The primary record is now an **observed quote record** (§5). Modeled impact columns are not the point of this milestone and are deferred. |
| "EVO-X2 was not contacted; the host is unreachable pending a Tailscale re-auth." | EVO is reachable and the sparse observation lane is **live**. This agent still made no measurement; the open items are now PENDING MEASUREMENT with named queries (§14) rather than unreachable. |
| Scope doc **Q3** — is a notional-parameterized probe acceptable? | **Resolved by the acceptance.** Eric's instruction says "Capture for fixed predeclared paper notionals". §4 is the preregistration that discharges it. |
| Scope doc **Q6** — a bounded paid SolanaTracker exception for slippage validation. | **Refused by the amendment**, not merely deprioritized: neither mode may use a paid trade/orderflow feed or SolanaTracker. It is no longer an open question; it would require a further amendment. |

### 0.4 A canon follow-up this milestone creates, stated non-silently

`app/canon.py:146-172` currently says of `READ_ONLY_ROUTE_QUOTE`:

> "NOT IMPLEMENTED; authorizes no milestone, and building it today still means
> STOP AND REPORT BACK (it requires a separately accepted milestone that **does
> not yet exist**)."

**That sentence is now false.** The milestone exists and is accepted (§0.1).
Canon and reality disagree, which is exactly the drift this repository amends
openly rather than reinterprets. **Required follow-up (§12, FU-1):**
`app/canon.py` and `AGENTS.md` must be updated to name
SOLANA-ROUTE-OBSERVATION-001 as the accepted milestone for
`READ_ONLY_ROUTE_QUOTE`. That is a canon change, must be reviewed as one, and
must **not** be smuggled into an implementation checkpoint.

---

## 1. Objective and success criterion

### Objective

Determine, prospectively and from recorded evidence only, **whether a
trustworthy `ExecutionQuote` for a Solana memecoin entry or exit can be built
from quantities observable without submitting a transaction** — and, where it
cannot, record the honest typed non-observation rather than a plausible number.

### This is a DECISION milestone

"The lane works" is not the claim under test. The claim is "we can quote". A run
that concludes **`execution_quote_not_trustworthy`, with evidence, is a SUCCESS
of this milestone** and a *block* on the paper-P&L milestone. That asymmetry is
the point, and the report is required to be able to express it (SC-5).

A milestone that can only conclude "yes" is not a measurement.

### Falsifiable success criteria

| id | criterion | falsified by |
|---|---|---|
| **SC-1** | **No failure is ever recorded as a success.** Every quote attempt carries a typed `quote_state`; every row in a success state carries a recorded HTTP status, a non-empty response digest, and a measured latency. | one row in a success state missing any of the three; one success row for a token whose attempt was recorded as failed or skipped in that pass's ledger |
| **SC-2** | **Zero paid spend.** Paid-provider calls attributable to this lane are zero over the window, and the run-scoped provider policy denied every paid provider **before a client or socket existed**. | any paid call; any SolanaTracker call; any request carrying a key |
| **SC-3** | **No number without its inputs.** Any field the venue did not return is a typed absence (§5.4) — never `0`, never interpolated, never carried from a previous quote. Every DERIVED field names its inputs and is absent when any input is absent. | one defaulted numeric; one non-null derived field with an absent input |
| **SC-4** | **The quote is reproducible-as-recorded.** For every persisted quote, recomputing the evidence digest from the persisted canonical fields reproduces the stored digest byte-for-byte. | one mismatch |
| **SC-5** | **The verdict is reachable in all three directions.** The end-of-window report emits exactly one of `execution_quote_trustworthy` / `execution_quote_trustworthy_with_stated_gaps` / `execution_quote_not_trustworthy`, derivable from persisted rows alone. | a report that cannot express "not trustworthy", or whose verdict needs an input outside the persisted rows |
| **SC-6** | **The notionals were not moved.** The ladder digest fixed at CP-1 (§4.5) equals the ladder digest on every persisted row and in the final report. | any row whose ladder digest differs from the preregistered one |
| **SC-7** | **The forbidden surface stayed empty.** No request in the window went to any route other than the declared quote route; no response containing transaction bytes was accepted (§7.4). | one request to any other route; one accepted response carrying an instruction/transaction field |

SC-1..SC-4 make the lane trustworthy. SC-5 makes it useful. SC-6 is what makes
this a preregistration rather than a story told afterwards. **SC-7 is what keeps
the boundary a boundary rather than an intention.**

---

## 2. What this unlocks — the exact dependency that disappears

The target ledger is one shared paper ledger across venues:

`Opportunity → PaperOrder → ExecutionQuote → PaperFill → Position → ExitDecision → ExitQuote → RealizedPaperPnL`

**Today that chain is broken at `ExecutionQuote`.** Several lanes in this
repository can already produce an `Opportunity`-shaped observation — the sparse
observer, the tape, the risk engine — and **none** of them can say what a stated
size would actually cost. Without that, a `PaperFill` could only ever be
"mid price × size", which is a fabricated measurement wearing the clothes of an
observed one: the exact failure class this repository has spent five milestones
closing.

**When this milestone lands with a positive verdict, exactly one dependency
disappears: the `ExecutionQuote` node stops being unobtainable and becomes an
OBSERVED artifact** — an executable output amount, a price impact, a fee and a
route for a declared input size, at a recorded instant, with a digest that proves
it was not altered afterwards.

Stated precisely, so it is not oversold:

- **Disappears:** "we have no observed basis for an entry or exit price at size."
- **Does NOT disappear:** `PaperFill` stays gated on MVP-005B. Even with a
  positive verdict, a fill remains a **MODELED** artifact (§8.3), because
  landing probability and adversarial extraction are unobservable without
  submitting. This milestone makes the fill model's *inputs* observed; it does
  not make the fill observed. Under `PAPER_SIMULATION` such a fill would have to
  carry a model identifier and a modeled-vs-observed basis on the artifact
  itself — and this milestone builds no such artifact.
- **Does NOT disappear:** `ExitQuote` is the same capability at a later instant,
  so the same result unlocks it — but only because the ladder is **bidirectional**
  (§4.3). A one-directional ladder would leave the exit half of the ledger just
  as unobtainable as before, which is why bidirectionality is preregistered
  rather than optional.
- **Does NOT disappear:** `Opportunity` selection, sizing, and any notion of
  which trade to make. Nothing here produces or implies a side.

A negative verdict is equally load-bearing: it tells the roadmap that the first
trustworthy prospective paper P&L is **not** reachable on free public data, and
that the next decision is a spend decision for Eric, not an engineering task for
an agent.

---

## 3. The capability boundary

The source of truth is `docs/SAFETY_BOUNDARIES.md`, amendment
**SAFETY-BOUNDARY-ROUTE-QUOTE-001 (2026-08-14)**, together with `app/canon.py`
`NARROWLY_PERMITTED_MODES`. Quoted, not paraphrased:

> `READ_ONLY_ROUTE_QUOTE` — may **retrieve executable route/amount evidence**:
> what route a stated input size would take, and what output amount, price
> impact, and fee a public quote endpoint reports for it. Retrieval of the quote
> and nothing else.

### 3.1 PERMITTED under this milestone

| # | permitted | grounding |
|---|---|---|
| P1 | A **quote request** to a **free public** quote endpoint for a stated input size | amendment, `READ_ONLY_ROUTE_QUOTE`; Eric's "Allowed: quote request" |
| P2 | Receiving and recording the **executable route response** — output amount, price impact, fee, route, pools, split | amendment; Eric's "executable route response · route/pool analysis · price impact observation · fee observation" |
| P3 | **Analysis** of that route/pool composition | Eric's "route/pool analysis" |
| P4 | Recording **context slot**, **request latency**, and an **evidence digest** | Eric's capture list |
| P5 | A non-GET request **only if** the venue exposes quoting only that way — carrying no key, no wallet, and nothing that mutates venue state, and still not returning instructions | amendment, verbatim: "If a venue exposes quoting only via a non-GET request, that request may carry no key, no wallet, and nothing that mutates venue state — and it still may not return instructions" |

### 3.2 FORBIDDEN — enumerated, because the inference is one step away

The amendment closes the "so just fetch the instructions" inference "by
enumeration, not by trusting a later reader's judgment". Under this milestone
there is **no implementation surface** for:

| # | forbidden | note |
|---|---|---|
| F1 | **The build/swap sibling route of the very same API that served the quote** | named explicitly in the amendment: "including the build/swap sibling route of the very API that served the quote. Reaching a quote route grants nothing on any other route." A quote endpoint being reachable says **nothing** about its sibling. |
| F2 | Requesting, fetching, or receiving **swap instructions, a serialized transaction, or transaction/instruction bytes** from any endpoint | also forbidden to *hold* such bytes in-process however obtained |
| F3 | **Constructing, assembling, encoding, or serializing** a transaction, instruction, or message by any means, **client-side included** | |
| F4 | **`simulateTransaction`** and equivalents against an RPC node | amendment: "that is transaction construction with a different verb, and it requires the bytes this amendment forbids obtaining". **This is the highest-fidelity non-executing estimator on Solana, and this milestone deliberately forgoes it.** That is a real fidelity cost, recorded here so it is not rediscovered later as a surprise. |
| F5 | **Signing** anything, with any key | |
| F6 | **Submitting, broadcasting, sending, or relaying** a transaction — and **fetching a blockhash, a priority fee, or a nonce** | the *fetches* are forbidden independently of what would be done with them. `getRecentPrioritizationFees` and equivalents are out. |
| F7 | **Loading, deriving, generating, importing, holding, or referencing wallet key material**, seed phrases, or keypairs | KALSHI-READONLY-AUTH-001 is confined to Kalshi read-scoped request authentication in `app/realtime/auth.py` and **does not extend here** |
| F8 | Supplying **a wallet address we control** as the quote's user/payer, or **any parameter whose only function is to bind the quote to OUR ability to execute it** | "The permitted object is *what a trade of size X would cost*, never *the trade we are about to make*." |
| F9 | **Any paid provider**: paid RPC, paid trade/orderflow feed, **SolanaTracker** | "free public endpoints only… A route quote obtainable only by paying for it is not obtainable under this amendment; the correct outcome is no quote, reported honestly, never a purchase." SolanaTracker's paid authorization in the discovery/risk lanes is scoped to those lanes and is **not a precedent this mode may borrow**. |
| F10 | A real fill, a real order, a real position, **real capital** | Eric: "Forbidden: … capital" |
| F11 | A **`PaperFill`, `PaperOrder`, `Position`, or `RealizedPaperPnL`** row, table, column, or placeholder | §11; `docs/SAFETY_BOUNDARIES.md` bans "disabled" and "placeholder" versions |

### 3.3 Three design consequences that fall directly out of the boundary

**From F8 — omit the account parameter entirely.** Some quote endpoints accept
an optional user/payer field. This milestone must **omit it** — not pass a
placeholder, not a burner, not a zero address chosen to look harmless, because
each of those is still a parameter whose only function is to bind the quote to
an executing account. If a venue *requires* such a parameter to return a quote,
then under this boundary **that venue cannot be quoted**, and the correct output
is the typed non-observation `quote_requires_account_binding` — not a workaround.
Whether the candidate endpoint requires it is PENDING MEASUREMENT (§14, M4).

**From F9 — "no quote" is an acceptable answer.** If the only reachable quote
endpoint requires a key or a paid tier, the milestone terminates at CP-0 with
`quote_unobtainable_free`. That is a **successful** termination of a decision
milestone, and the next decision belongs to Eric, not to an implementer.

**From F1/F2 — the client must be route-locked, not merely well-behaved.** The
implementation may not hold a general-purpose "call this API" client with a path
parameter. The single permitted route is a module constant; there is no path
argument on the public surface. This mirrors the containment reasoning already
proven in `app/realtime/auth.py`, where the signable input has no method and no
path parameter precisely so a second route cannot be reached by one call. See
§7.4 and CP-2.

### 3.4 What the amendment does NOT do

- It **authorizes no milestone by itself**: "it authorizes no milestone,
  installs nothing". The authorization for *this* milestone is Eric's B4
  instruction (§0.1), which is a separate act.
- It **does not amend the AST safety audit**. See §12, FU-2: an implementation
  **will FAIL** `frontier-eval-report --include-safety`, and that is the designed
  outcome, not a bug to route around.
- It **does not loosen** the "Swaps / transaction construction / signing (Jupiter
  or any DEX)" row of `docs/SAFETY_BOUNDARIES.md`. `READ_ONLY_ROUTE_QUOTE` sits
  *adjacent* to that row and "loosens no part of it".

---

## 4. THE PREDECLARED NOTIONALS — this section is the preregistration

Eric's instruction: *"Use fixed notionals declared BEFORE evaluating results. Do
not optimize notionals after seeing favorable execution."*

This section discharges that instruction. **It is declared before any quote has
been requested, because none has: no provider call has been made for this
milestone by anyone.**

### 4.1 The ladder

Four rungs, USD-equivalent, spanning roughly two orders of magnitude:

| rung | USD-equivalent | share of the repo's own $5,000 "minimum interesting liquidity" | share of the $1,000,000 quality-score saturation point |
|---|---|---|---|
| **N1** | **$25** | 0.5% | 0.0025% |
| **N2** | **$100** | 2% | 0.01% |
| **N3** | **$500** | 10% | 0.05% |
| **N4** | **$2,000** | 40% | 0.2% |

Spacing is approximately half-decade (4x, 5x, 4x). Four points across two decades
is the minimum that can distinguish a roughly linear impact curve from a
convex one; three points cannot, and five costs 25% more requests for a
discrimination the first four already provide.

### 4.2 Why these four, from the liquidity the repository already commits to

The two anchors are **in this repository**, not invented for this document:

- `app/config.py:369` `crypto_min_liquidity_usd: float = 5000.0` and
  `app/config.py:561` `crypto_risk_min_liquidity_usd: float = 5000.0` — the
  scout and risk lanes both treat **$5,000 of pool liquidity** as the threshold
  below which a token is not worth acting on. That is this project's already-
  committed opinion about the thin end of the distribution.
- `app/services/crypto_horizon.py:432` `min(liq, 1_000_000) / 10_000` — the
  pair-quality score **saturates at $1,000,000**. That is this project's
  already-committed opinion about where additional depth stops mattering.

The sparse observation lane deliberately applies **no** liquidity threshold at
all (it preserves the whole eligible birth population as its denominator), so
the population being quoted spans from far below $5,000 to far above it. A
ladder anchored only at the top would be uninformative on most of the
population; one anchored only at the bottom would never reach the sizes an entry
would actually use.

Rung by rung:

- **N1 $25 — the fixed-cost probe.** Deliberately below any plausible entry. Its
  job is to isolate the size-**independent** components of cost (network base
  fee, any flat aggregator fee, any account-creation cost the venue quotes) from
  the size-**dependent** impact. It is the control rung. If price impact is
  already material at $25, the pool is untradeable at every larger size, and
  that is a finding, not a failure.
- **N2 $100 — the smallest plausibly-real size.** The rung at which fixed Solana
  costs plausibly stop dominating the total. 2% of the $5,000 floor.
- **N3 $500 — the discriminating rung.** Exactly 10% of the project's own
  minimum-interesting-liquidity constant. For the thinnest pool this repository
  considers worth looking at, a $500 print is unambiguously material; for a
  $1M pool it is noise. **If any single rung decides the milestone, it is this
  one**, because it is where the impact curve should separate tokens.
- **N4 $2,000 — the deep-end probe.** 40% of the $5,000 floor — which is to say,
  catastrophic in a thin pool, and that catastrophe *is* the measurement — and
  0.2% of the saturation point, i.e. still small where depth is real.

**What is explicitly NOT claimed:** that these are good position sizes, that
they are sizes we intend to trade, or that they were derived from any signal,
conviction, capital base, or token-specific input. They are **fixed constants of
the measurement instrument**, chosen from two constants already in the
repository, in the same sense that a bid-ask spread is a property of a book. No
code reads them to decide anything. `docs/SAFETY_BOUNDARIES.md` keeps portfolio
sizing forbidden and this changes nothing about that: "a size is a stated INPUT
of the simulation… it is not a sizing recommendation, and nothing may derive,
optimize, rank, or recommend a size from a modeled result."

### 4.3 Denomination — exact integers, no price feed, no drift

**The entry input mint is a single declared USD stablecoin mint, and each rung is
expressed as an EXACT INTEGER of that mint's base units.**

This matters more than it looks. If the ladder were denominated in SOL, each
rung's dollar value would drift with the SOL price, the notional would silently
differ between the first pass and the last, and "fixed predeclared notionals"
would quietly become "fixed predeclared *token* amounts of varying value". A
stablecoin denomination makes each rung an integer constant that cannot move.

- The mint address and its decimal count are **PENDING VERIFICATION** at CP-0
  from a free public source (§14, M5). Nothing in this repository records them,
  so this document does not assert them.
- Once verified, the four integers are computed **once**, written into the
  ladder record, and enter the ladder digest (§4.5). They are never recomputed
  at runtime and never derived from a live price.

**Bidirectionality — the exit rung is DERIVED, and derived from an OBSERVED
quantity.** For each rung, the exit-side quote's input amount is the **exact
integer `amount_out` returned by that same rung's entry quote in that same
pass**. This is the definitional round trip: it asks "if I put $X in and got Y
tokens, what do I get back for exactly those Y tokens?"

- The exit input amount is therefore **DERIVED**, labelled as such (§5), and is
  a **typed absence** whenever the entry quote for that rung was a
  non-observation. There is no fallback exit amount, and in particular the
  previous pass's `amount_out` is never substituted.
- This is not "optimizing a notional after seeing execution". The exit amount is
  fully determined by a rule fixed here, before any quote exists; no human or
  agent chooses it, and it cannot be re-chosen to look better.
- Without this, `ExitQuote` stays exactly as unobtainable as it is today (§2),
  so bidirectionality is preregistered rather than optional.

### 4.4 The population is preregistered too — otherwise the ladder is theatre

Freezing the notionals while leaving token selection free would move the
cherry-picking one level up. So:

- **Population:** every token-horizon observation the sparse lane records in the
  pass, in a deterministic order (ascending observation id), with **no**
  liquidity, quality, venue, risk, or outcome filter — matching the sparse
  lane's own denominator-preserving rule.
- **Bound:** a declared per-pass cap `ROUTE_QUOTE_MAX_TOKENS_PER_PASS`, applied
  by that deterministic order and **never** by any property of the token. A
  truncation is recorded as `population_truncated` with the true count, so a cap
  can never be mistaken for a complete population.
- **Forbidden:** excluding a token because its quote failed, looked bad, or was
  slow. A failed quote is a row with a typed failure state, not an absence.

### 4.5 FROZEN — and what unfreezing costs

The ladder record is:

```
ladder_id      : "SRO001-LADDER-V1"
rungs_usd      : [25, 100, 500, 2000]
entry_mint     : <verified at CP-0>
entry_decimals : <verified at CP-0>
rungs_base_units : [<computed once at CP-1>]
direction_rule : "exit input = entry amount_out, same rung, same pass"
population_rule: "all sparse-lane observations in pass, ascending observation id, capped"
ladder_digest  : sha256 over the canonical encoding of the above
```

**These notionals are FROZEN. Changing them after any quote has been evaluated
invalidates the measurement**, and there is no version of that change that
merely "improves" it: a ladder chosen after seeing which rungs produced
favourable execution is not a measurement of execution cost, it is a report of
which sizes happened to look good, and it cannot be distinguished from the
former after the fact by anyone reading the results.

Enforcement, not intention:

- `ladder_digest` is written on **every persisted quote row** (§5). SC-6 fails
  the milestone if any row disagrees with the preregistered value.
- The final report prints the ladder digest next to the verdict. A verdict
  whose rows carry more than one ladder digest is **reported as invalid**, not
  reconciled.
- **One amendment window exists, and it closes at the first quote.** Between
  CP-0 and CP-1 the rungs may be amended **once**, on Eric's explicit written
  approval, and only on grounds that contain no quote result — for example, the
  CP-0 liquidity distribution showing the population is entirely outside the
  ladder's useful range. Any such amendment is recorded here **as a new
  `ladder_id` alongside the old one, with both digests and the reason**; nothing
  is edited in place. After the first quote request is issued, the ladder is
  frozen absolutely and this window does not reopen.
