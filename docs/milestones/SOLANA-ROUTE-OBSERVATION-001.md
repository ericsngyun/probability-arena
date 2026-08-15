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

---

## 5. The evidence record

One row per **(observation, rung, direction)**. Every field in Eric's capture
list appears below with its source, its OBSERVED / DERIVED / MEASURED-LOCAL
classification, and its typed non-observation.

### 5.1 A deliberate divergence from the sparse lane, stated non-silently

The sparse observation lane writes **no row** when a request fails. **This lane
writes a row.** The divergence is deliberate: the sparse lane's product is an
honest denominator of token observations, and a failed request there is not an
observation about the token. This lane's product is *whether quoting is possible
at all*, so **the failure rate is the primary evidence** — a quote that could not
be obtained is exactly the finding the milestone exists to record. A failed
attempt therefore produces a row whose `quote_state` is a failure state and
whose every quantity field is a typed absence.

This is an architectural difference from the pattern being reused, and it is
recorded here rather than discovered in review.

### 5.2 Identity, timing and request fields

| field | O/D | source | typed non-observation |
|---|---|---|---|
| `observation_id` | — | FK to the sparse-lane observation this quote accompanies; inherits its (cohort, token, horizon) uniqueness | never absent — no row exists without it |
| `chain`, `token_address`, `horizon` | — | denormalized from the observation | never absent |
| `rung_id` | — | one of `N1`..`N4` (§4.1) | never absent |
| `direction` | — | `entry` or `exit` | never absent |
| `ladder_id`, `ladder_digest` | — | §4.5, constant per row | never absent; a row without them is not written |
| `observed_at` | MEASURED-LOCAL | the request instant on the logical clock, matching the sparse lane's `_logical_clock` discipline | never absent |
| `quote_state` | — | closed vocabulary, §5.5 | never absent |
| `http_status` | OBSERVED | the response status; absent when no response arrived | `no_response` |
| `request_latency_ms` | MEASURED-LOCAL | monotonic clock around the request; a property of **our measurement**, not of the venue | absent only when the request was never issued (`request_not_issued`) |
| `context_slot` | OBSERVED | **only** if the quote response itself carries it. **No separate RPC call is made to obtain a slot** — that would add a provider for a field the venue may already give us free, and it sits adjacent to the forbidden blockhash/nonce fetches (§3.2 F6). | `not_returned_by_venue` |
| `evidence_digest` | DERIVED | §6 | never absent on a row with a response |

### 5.3 Quantity fields — Eric's capture list

| field | O/D | source | typed non-observation |
|---|---|---|---|
| `token_in` | — | the declared entry mint (entry) or the observed token (exit) | never absent |
| `token_out` | — | the observed token (entry) or the declared entry mint (exit) | never absent |
| `input_amount` | **entry: declared** / **exit: DERIVED** | entry = the frozen rung integer (§4.3); exit = the exact `amount_out` integer of the same rung's entry quote in the same pass | exit side: `derivation_input_absent` when that entry quote is not an observation. **No fallback, and never the previous pass's value.** |
| `amount_out` | OBSERVED | the venue's quoted output, **exact integer base units** | `not_returned_by_venue` / `venue_returned_unparseable` |
| `min_or_threshold_out` | OBSERVED | the venue's minimum/threshold output **where provided**; many venues provide it only when a slippage tolerance is supplied | `not_returned_by_venue`. **A missing threshold is never synthesized from a slippage assumption** — that would be a modeled number in an observed column. |
| `executable_price_equiv` | **DERIVED** | `amount_out / input_amount`, decimal-normalized by both mints' decimals | `derivation_input_absent` if `amount_out` is absent **or** either decimals value is unverified. §4.3 M5. |
| `price_impact` | OBSERVED | the venue's reported price-impact figure, stored as canonical decimal **text** | `not_returned_by_venue`. **Never computed from our own mid price and presented in this column** — a derived impact is a different quantity and would need its own DERIVED field and a model id. |
| `fees` | OBSERVED | whatever fee components the venue reports (platform fee, per-hop pool fee), each stored with its own label; the set of components is **whatever the venue named**, never a filled-in template | per component: `not_returned_by_venue`. **No default fee, no assumed bps, no per-dex fee table.** |
| `route` | OBSERVED | the ordered hop list from the response | `route_not_returned`; if no route exists for the size, `no_route_for_size` (a real, informative answer, not a failure) |
| `dexes_pools` | OBSERVED | per hop: the venue's pool/market identifier and its dex label, verbatim | `not_returned_by_venue`; an unrecognized dex label is stored **verbatim and unmapped** — never bucketed into a guessed family |
| `route_split` | OBSERVED | per-branch proportions where the venue reports a split | `not_returned_by_venue`. **A single-branch route is recorded as a single branch, never as "100%" invented across an absent field.** |
| `route_hops` | DERIVED | count of hops in `route` | `derivation_input_absent` when `route` is absent |

### 5.4 Typed absence is a closed vocabulary, and it is the only permitted absence

**A missing field is a typed absence, never an interpolated or defaulted
number.** Concretely, and enforced by test at CP-2:

- Absences live in one bounded, fixed-key structure `absent_fields`, mapping a
  field name to exactly one reason from this closed set:
  `not_returned_by_venue` · `venue_returned_null` · `venue_returned_unparseable`
  · `derivation_input_absent` · `route_not_returned` · `no_route_for_size` ·
  `no_response` · `request_not_issued` · `route_too_large` ·
  `quote_requires_account_binding` · `population_truncated`.
- **Free text is not permitted** in that structure. An unanticipated shape is
  `venue_returned_unparseable`, which is honest, rather than a new sentence,
  which is unbounded cardinality.
- `0`, `0.0`, `""`, `"unknown"`, `-1`, and "the previous pass's value" are all
  **forbidden** stand-ins. Zero is an affirmative claim; absence is not a claim.
- A field that is absent has its column NULL **and** an entry in `absent_fields`.
  Neither alone is valid: a NULL with no reason is indistinguishable from a bug,
  and a reason with a non-NULL value is a contradiction. CP-2 tests both
  directions.

### 5.5 `quote_state` — the closed state vocabulary

`quoted_ok` · `quoted_no_route` · `quote_partial_fields` · `request_failed` ·
`response_unparseable` · `identity_mismatch` · `stale_context` ·
`quote_requires_account_binding` · `skipped_cap` · `skipped_flag_off` ·
`refused_forbidden_response` (§7.4).

Only `quoted_ok` and `quoted_no_route` are answers. `quoted_no_route` is a real
answer — the venue says no route exists for that size — and is one of the more
informative outcomes this milestone can record.

### 5.6 Numeric typing — the truncation class is already known here

Amounts are large integers in base units and price impact is a small decimal.
**Neither may pass through a Python `float` at any point**, including parsing.
KALSHI-ARCHIVE-REPLAY-INTEGRITY-001 found silent decimal truncation of ordinary
venue JSON *under a valid digest* — a digest computed over already-truncated
values proves only that the truncation was not altered afterwards.

The repository already has the fix: `app/realtime/canonical.py` parses with
`parse_float=Decimal` / `parse_int=int` and emits `Decimal` as canonical decimal
text that round-trips to the same bytes. This lane **reuses that module** rather
than introducing a second canonicalization. Amounts are stored as exact integers
(or canonical integer text); impact and fee rates as canonical decimal text.
CP-2 carries an explicit test that a value which would truncate under `float`
survives intact.

### 5.7 Storage shape and growth

Row arithmetic: 4 rungs × 2 directions = **8 rows per token-horizon
observation**. The sparse lane's own planning rate implies on the order of a
thousand observations per day, which would be on the order of 8,000 rows/day if
every observation were quoted — on a database already past its 3,072 MiB gate.

Therefore:

- `ROUTE_QUOTE_MAX_TOKENS_PER_PASS` is a declared, deliberately small cap for
  the activation window (§4.4), applied by deterministic order and never by any
  property of the token, with truncation recorded as `population_truncated`.
- The **route structure is stored in one bounded canonical column with a hard
  byte cap**. Exceeding the cap yields the typed absence `route_too_large` — it
  is **not** truncated and presented as complete. (R7 in the scope doc's
  fabrication catalogue: truncation presented as completeness.)
- **No raw response body is persisted.** RAW-PAYLOAD-STORAGE-001 measured raw
  payloads at 27% of the production database with zero readers; re-inflating
  them here is the mistake this repository has just finished undoing. The digest
  (§6) is what preserves integrity, and it is 32 bytes.
- The exact current DB headroom is **PENDING MEASUREMENT** (§14, M3); the cap
  must be set from that number, not from this document's arithmetic.

---

## 6. The evidence digest — what exactly is hashed

Eric's capture list ends with "evidence digest". Its job is narrow and worth
stating precisely: **to make it demonstrable that a recorded quote was not
altered, filled in, or re-attributed after the fact.**

### 6.1 Two digests, because one cannot do both jobs

| digest | over what | what it proves | recomputable later? |
|---|---|---|---|
| `response_body_digest` | the **exact response bytes as received**, hashed **before parsing** | our parse is attributable to a specific body; a parser change can be re-audited against a body captured in a dry run | **No** — the body is not persisted (§5.7). It is a commitment, not a re-derivation. |
| `evidence_digest` | the **canonical encoding of the persisted field set**, including `response_body_digest` | the persisted row is exactly what was persisted at observation time | **Yes** — this is what SC-4 checks |

Chaining the body digest *into* the evidence digest is what stops the second
from degenerating into "a hash of whatever we decided to store". Neither digest
alone is sufficient: hashing only the body gives a number nobody can recompute
from the database; hashing only the persisted fields proves nothing about
fidelity to the wire.

### 6.2 The exact preimage of `evidence_digest`

A **closed** key set — adding, removing or renaming a key requires bumping
`digest_version`, which is itself in the preimage:

```
digest_version            (constant, e.g. "sro001.v1")
ladder_id, ladder_digest
chain, token_address, horizon, observation_id
rung_id, direction
request:
  route_name              (the single permitted route constant, §7.4)
  token_in, token_out
  input_amount            (exact integer, as canonical integer text)
  request_params          (the complete, closed set of parameters actually sent)
observed_at               (canonical datetime)
http_status
request_latency_ms
response_body_digest
parsed:
  amount_out, min_or_threshold_out, price_impact, fees,
  route, dexes_pools, route_split, route_hops, context_slot
derived:
  executable_price_equiv
quote_state
freshness_basis
absent_fields             (the full typed-absence map, §5.4)
```

### 6.3 The rules that make it mean something

1. **Encoding is `app/realtime/canonical.py`**, not a new one. Deterministic key
   order, `Decimal` as canonical decimal text that round-trips to identical
   bytes, integers as integers, **no floats anywhere in the preimage**. A second
   canonicalization in this repository would be a second chance to disagree with
   the first.
2. **`request_params` is the complete set actually sent**, not the set we
   intended to send. This is what makes the digest evidence that the quote was
   for *this size, this pair, this route*, and it is also what would expose a
   forbidden parameter (§3.2 F8) rather than hiding it.
3. **Absences are in the preimage, not omitted.** Committing to "we did not
   observe `price_impact`" is what makes a later backfill detectable. If
   absences were simply left out, filling one in afterwards would be
   indistinguishable from having observed it.
4. **Nothing database-assigned is in the preimage** — no row id, no
   `created_at`. Otherwise SC-4 could not recompute the digest from the row.
5. **The digest is computed once, at observation time, in the same pass**, and
   never recomputed on read. A recompute-on-read digest proves only that the
   code is self-consistent.
6. A row whose digest cannot be computed is **not written**. There is no
   `digest = NULL` row and no `digest = "pending"`.

### 6.4 What it does not prove

It does not prove the venue told the truth, that the route was executable, or
that a transaction would have landed. It proves only that **we recorded what we
recorded, when we said we did.** That is the entire claim, and overstating it
would be the same category of error this milestone exists to avoid.

---

## 7. Identity and freshness invariants

These mirror the sparse lane's proven pattern
(`app/services/crypto_sparse_observation.py`, `_identity_matched`), which exists
because a chain-correct, well-formed response *about a different token* would
otherwise parse, score, and persist cleanly — a wrong number being worse than a
missing one for a lane whose only product is honest evidence.

### 7.1 Identity — a quote for token T must concern token T

- **Endpoint mints are compared byte-exactly** against the requested mints,
  case-sensitive. Base58 addresses are case-significant; a case-insensitive
  compare is a real collision risk, not pedantry.
- **Direction-aware:** for `direction = entry`, the response's output mint must
  be exactly T; for `direction = exit`, the response's input mint must be
  exactly T. A response where T appears on the *other* side is **not** identity —
  it prices some other asset — exactly as a quote-side pair is not identity in
  the sparse lane.
- **The other endpoint is checked too:** it must be exactly the declared entry
  mint. A quote from a different stablecoin is a different measurement.
- **Intermediate hops are exempt by design.** A route may legitimately pass
  through arbitrary mints; identity is a property of the two endpoints only.
  Stated explicitly so a reviewer does not reject correct multi-hop routes.
- On failure: `quote_state = identity_mismatch`, **no quantity field is
  persisted at all**, and the row's numbers are absences with reason
  `venue_returned_unparseable`. As in the sparse lane, an identity mismatch is
  indistinguishable from an upstream contract violation, so it is re-attemptable
  within the pass under a hard attempt cap — it is not silently retried forever.

### 7.2 Freshness — a stale quote is a typed non-observation, not a number

A quote is perishable in a way a price tick is not: it asserts what a *specific
size* would get *right now*.

- **`request_latency_ms` ceiling.** A declared, frozen ceiling. A response that
  arrives after it describes a chain state we can no longer bound, so
  `quote_state = stale_context` and **no quantity field is persisted**. The
  latency is still recorded, because the latency distribution is itself evidence
  about whether quoting is viable.
- **Entry/exit pairing bound.** The exit quote's input is derived from the entry
  quote's output (§4.3). If the interval between the two requests exceeds a
  declared bound, the pair is not a round trip and the **exit** row is
  `stale_context`. The entry row remains valid; one stale half does not
  retroactively invalidate an honest observation.
- **`context_slot`, when the venue returns it**, gives a second, better freshness
  handle: the slot delta between paired entry and exit quotes. Beyond a declared
  bound, `stale_context`.
- **`freshness_basis`** is a recorded field valued `context_slot` or
  `latency_only`. When the venue does not return a slot, the weaker basis is
  **recorded as weaker**, never presented as if the stronger check had run. No
  RPC call is made to obtain a slot (§5.2).

### 7.3 No substitution, ever

No quote is reused across passes, rungs, or directions. Every row's numbers come
from that row's own response. There is no backfill, no interpolation, no
nearest-in-time substitution, and no "the last quote was basically the same".
A pass with no answer produces failure-state rows, which is the honest signal.

### 7.4 The route lock and the forbidden-response refusal

Two structural defenses for SC-7, both of which must exist in code rather than
in discipline:

- **Route lock.** Exactly one route constant exists in the module. The client's
  public surface has **no path or endpoint parameter**, so a second route — in
  particular the build/swap sibling (§3.2 F1) — cannot be reached by supplying
  an argument. This is the same containment shape already proven in
  `app/realtime/auth.py`, where the signable input carries no method and no path
  precisely so one call cannot reach a second route.
- **Forbidden-response refusal.** If a response contains any field carrying
  transaction or instruction bytes — we never ask for them, but a venue can
  change what it returns — the client **refuses the entire response**: it
  persists `quote_state = refused_forbidden_response`, persists no quantity
  field, does not retain the bytes, and does not compute a body digest over
  them. "We only ignored the field we were not allowed to have" is not a
  boundary; refusing the response is.

---

## 8. What is observable, and what is not

Carried forward from the scope doc and **updated for the amendment**. To make a
later `ExecutionQuote` trustworthy you need, at the quote instant: a reference
price, the depth at it, the impact of a print of a given notional, the route,
the fees, the realized slippage between quote and fill, the landing probability
and slot delay, and the adversarial cost.

### 8.1 The split, after the amendment

| # | quantity | status now | changed by the amendment? |
|---|---|---|---|
| 1 | mid price | **OBSERVABLE** — DexScreener, already fetched by the sparse lane | no |
| 2 | depth | **OBSERVABLE only as a provider-computed USD aggregate**; true reserves remain unparsed and unverified | no |
| 3 | **price impact at a given notional** | **OBSERVED from the quote response** (§5.3) | **YES — was "ESTIMATED ONLY", with unbounded error for concentrated-liquidity pools** |
| 4 | **route composition — pools, split, hops** | **OBSERVED from the quote response** | **YES — was "NOT OBSERVABLE; proxy only"** |
| 5a | pool fee per hop | **OBSERVED where the venue reports it**; otherwise a typed absence, never a per-dex default table | **YES, partially** |
| 5b | Solana base fee / priority fee | **NOT AVAILABLE** — fetching a priority fee is explicitly forbidden (§3.2 F6) | no; now forbidden by name rather than merely absent |
| 5c | associated-token-account rent | **NOT OBSERVED**; moot without a wallet, real for any future quote | no |
| 5d | token-2022 transfer fee / hooks | **NOT OBSERVABLE** from these sources. If present and unaccounted, every quote is wrong by an unknown multiplicative factor — a **silent-wrongness** risk, so it is recorded as an absence on every row rather than omitted | no |
| 6 | realized slippage | **NOT OBSERVABLE prospectively.** Retrospective measurement needs a per-trade feed, which is **paid and now explicitly forbidden** (§3.2 F9) | **YES — went from "expensive" to "out of bounds"** |
| 7 | landing probability, slot delay | **NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION** | no |
| 8 | MEV / sandwich extraction | **NOT OBSERVABLE WITHOUT SUBMITTING A TRANSACTION** | no |
| 9 | exit-side depth at t+Δ | **OBSERVABLE** as a later observation — what the sparse lane already buys | no |

### 8.2 The three that did not move, and why they matter most

- **Landing** (7) and **adversarial extraction** (8) are unobservable by
  construction: no provider sells you your own counterfactual, and MEV is a
  response *to your own order*, which does not exist until you send it.
- **Realized slippage** (6) was the one quantity that could have validated a
  fill model against something resembling ground truth. The amendment closes the
  only route to it. **This milestone therefore has no ground truth to validate
  against and cannot acquire one within its boundary.** That is a permanent
  limitation of the result, not a gap more work will close.

### 8.3 The finding, restated and now sharper

> **Any `PaperFill` this project ever writes is a MODEL OUTPUT, never a
> measurement** — because landing and adversarial extraction cannot be observed
> without executing.

The amendment agrees and turns the finding into a hard requirement: under
`PAPER_SIMULATION` a modeled fill **must** carry an explicit model identifier
and a modeled-vs-observed basis *on the artifact itself* — not in a header,
README, docstring, or column comment, because none of those survive the number
being copied into a report or another agent's context, "which is exactly when
the mislabeling happens".

What this milestone changes is the *basis*: with observed quote evidence, such a
model's inputs become OBSERVED rather than assumed. The fill stays MODELED.

### 8.4 The fallback if quoting turns out to be unobtainable

If CP-0 finds no free public quote endpoint that returns a quote without an
account binding (§3.3), the milestone terminates with `quote_unobtainable_free`.
The scope doc's proxy design — pool-inventory composition plus a declared,
conservative, NULL-when-inputs-are-absent impact model over provider TVL —
stays on record as a **separate, smaller, separately-approved** fallback
milestone. It is deliberately **not** folded into this one: a proxy shipped
under this milestone's name would later be read as if this milestone had
succeeded.

---

## 9. Implementation checkpoints

Repo pattern: dry-run → focused independent reviews → dark deployment →
bounded prospective activation. **Every checkpoint defaults OFF, and dry-run
comes first at every stage.** Each is independently verifiable and each can
terminate the milestone.

Reversibility tier assigned at design time: **1** autonomous · **2** single
confirmation · **3** dual confirmation.

### CP-0 — Entry gates and endpoint reconnaissance. **NO PRODUCTION CODE.** (Tier 2)

Two entry gates from Eric's own instruction, neither verified by this document:

- **G1** — 6h sparse-lane mechanics demonstrably healthy (§14, M1).
- **G2** — 24h jobs being scheduled correctly (§14, M2).

Then, by **hand-invoked, single-token, hard-capped, local** requests — not on
EVO, not scheduled, not behind any flag, nothing persisted to the production
database:

1. Does a **free public** quote endpoint return a quote **without** a key and
   **without** an account/user parameter? If it requires either, the answer is
   `quote_unobtainable_free` and the milestone terminates here (§3.3).
2. Which of the §5.3 fields does the response actually carry — output amount,
   min/threshold output, price impact, per-hop fee, route, pools, split, context
   slot? Each answer is a committed fixture, not a claim in prose.
3. The published rate limit, and whether it is compatible with the per-pass cap.
4. The declared entry mint's address and decimals (§14, M5).
5. Current DB headroom against the 3,072 MiB gate (§14, M3).

**Deliverable:** committed fixtures plus a findings section appended to this
document. **Gate:** any of 1–3 failing terminates the milestone as a successful
decision, not a failure.

### GATE-FU2 — the safety-audit unban. **(Tier 3 — dual confirmation)**

**This gate sits between CP-0 and CP-2 and cannot be skipped.** Writing the
implementation will FAIL `frontier-eval-report --include-safety` (§12, FU-2).
The correct response is a separate, narrow, separately-reviewed allowlist change
scoped to the exact fragment in the exact file, recorded in
`docs/SAFETY_BOUNDARIES.md` — **never** renaming an identifier to slip past the
scan, and never broadening the allowlist past the one file that needs it. Until
this gate passes, a failing safety audit is the correct state and not a defect.

### CP-1 — Contract, migration, ladder freeze. Dry-run only. (Tier 2 — schema change)

One additive table (§5), one migration with an up/down round-trip test, the
closed `quote_state` and `absent_fields` vocabularies, the digest version
constant, and one flag defaulting **false**. The ladder integers are computed
once, and the ladder digest is fixed and written back into §4.5. Import-time
invariant checks use `raise`, not `assert` — `python -O` strips asserts.
Nothing is wired to anything; `--dry-run` computes, prints, and persists nothing.

### CP-2 — Pure parsing and derivation, **no I/O**. (Tier 1)

A function from (recorded response bytes, request record) to a typed record. No
session, no network, no clock. Tests: one per fabrication shape; the
absence/NULL biconditional (§5.4); the no-float truncation test (§5.6);
direction-aware identity (§7.1); digest recomputation (SC-4); and a test that a
response containing transaction bytes is **refused whole** (§7.4).

### CP-3 — The route-locked client, against fixtures only. **Zero live calls.** (Tier 1)

The client with exactly one route constant and no path parameter (§7.4), tested
entirely against CP-0 fixtures. One test asserts the public surface has no path
or endpoint argument. One asserts the run-scoped provider policy denies every
paid provider **before a client or socket exists**.

### CP-4 — Wire into the pass, flag OFF. (Tier 2)

Assertions, not assurances: flag OFF yields a pass result byte-identical to
`main`; no read, no write, no compute, no external call; commit count unchanged;
the quote work never runs in a phase that structurally has no session; and the
sparse lane's own `external_calls` is unchanged.

### CP-5 — Three focused, independent reviews. All must PASS. (Tier 2)

| reviewer | charge |
|---|---|
| **fabrication / identity** | Can any failure be recorded as a success? Is any quantity field reachable with its input absent? Attack the absence vocabulary, the digest preimage, and the identity gate adversarially. |
| **storage / write-shape** | Rows/day against the DB gate; transaction count and lock hold; is anything persisting a raw body? |
| **boundary** | The `AGENTS.md` safety grep; `frontier-eval-report --include-safety`; a read of every column name asking "could this be read as a size, an EV, an order, or a recommendation?"; and an explicit hunt for any reachable path to the build/swap sibling route, any retained transaction bytes, and any account-binding parameter. |

### CP-6 — Dark deployment, flag OFF. (Tier 2)

On EVO: verify the no-op is a true no-op — no read, no write, no compute, no
external call, and zero rows in the new table.

### CP-7 — Bounded prospective activation, then verdict. (Tier 3 — dual confirmation)

Flag ON for a bounded, pre-declared window with a pre-declared per-pass cap.
This is the **first live use of a newly permitted capability**, which is why it
is Tier 3 rather than Tier 2. Then a report emitting exactly one verdict:

- `execution_quote_trustworthy` — observed fields complete and self-consistent
  often enough that a later `ExecutionQuote` may carry them with observed
  provenance.
- `execution_quote_trustworthy_with_stated_gaps` — usable, but named fields are
  systematically absent (no threshold output, no context slot, …), and those
  gaps travel with every downstream use.
- `execution_quote_not_trustworthy` — the free public inputs do not support a
  trustworthy `ExecutionQuote`. The paper-P&L milestone is blocked at this gate.

**All three are successful terminations of this milestone.**
