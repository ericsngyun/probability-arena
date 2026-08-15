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

### 4.1 The ladder — V2, re-anchored on the measured distribution

Four rungs, USD-equivalent. **These are the V2 rungs. V1 ($25 / $100 / $500 /
$2,000) was written before the liquidity distribution was measured and is
superseded; both versions are recorded in §4.5.**

| rung | USD | % of the **median** observed pool ($2,860) | % of p25 ($1,936) | % of p75 ($11,578) | % of p95 ($67,119) |
|---|---|---|---|---|---|
| **N1** | **$10** | 0.35% | 0.52% | 0.086% | 0.015% |
| **N2** | **$50** | 1.7% | 2.6% | 0.43% | 0.074% |
| **N3** | **$150** | 5.2% | 7.7% | 1.3% | 0.22% |
| **N4** | **$500** | 17% | 26% | 4.3% | 0.75% |

Spacing 5x / 3x / 3.3x, spanning 50x. Four points is the minimum that can
distinguish a roughly linear cost curve from a convex one; three cannot, and a
fifth costs 25% more requests — each rung is two requests, entry and exit — for
a discrimination the first four already provide.

### 4.2 Why these four — from the MEASURED liquidity of the quoted population

**The V1 anchor was wrong, and the measurement is what showed it.** V1 was
justified against `crypto_min_liquidity_usd = 5000.0` (`app/config.py:369`,
`:561`) as "the project's committed view of the thin end", and against the
$1,000,000 saturation point in `active_pair_quality_score`
(`app/services/crypto_horizon.py:432`) as the deep end. Both anchors turn out to
be in the wrong place for the population this milestone actually quotes.

Measured over the real quoted population — rolling cohort 8, n=42,
`liquidity_usd` at observation:

```
p0    $  1,592      p50   $  2,860      p90   $ 17,655
p5    $  1,633      p75   $ 11,578      p95   $ 67,119
p10   $  1,661                          p100  $167,041
p25   $  1,936      mean  $ 12,780
>= $5,000:   16 (38%)      >= $100,000:  1 (2%)
>= $25,000:   3 (7%)       >= $500,000:  0 (0%)
```

Three facts from that distribution drive the re-anchoring:

1. **62% of the population is BELOW the $5,000 floor.** The sparse lane applies
   no liquidity threshold by design, so its population is not the population the
   scout and risk lanes were tuned for. Anchoring on $5,000 anchored on a
   threshold that most of the sample fails.
2. **The $1,000,000 saturation point does no work at all.** The deepest observed
   pool is $167,041 — 6x below it, and nothing is within an order of magnitude
   of $500,000. An anchor no observation approaches is not an anchor.
3. **The distribution is sharply right-skewed and the bottom half is one
   regime.** p0 $1,592 to p50 $2,860 is a 1.8x spread across the entire bottom
   half, while p75 to p100 spans 14x. Mean ($12,780) is 4.5x median. So most of
   the population lives in a narrow thin band, and a ladder must discriminate
   *inside that band* or it discriminates nothing.

Against the median, V1's top two rungs were not quotes: **N3 $500 was 17% of the
median pool and N4 $2,000 was 70% of it — 126% of the thinnest observed pool.**
A quote for more than the pool contains is a hypothetical block trade, and it
returns a catastrophic answer for a structural reason (pool exhaustion) rather
than an informative one. Every thin token would have returned the same
uninformative verdict.

**The design criterion, made explicit: a rung must be non-degenerate.** It must
produce a spread of answers across the population rather than the same answer
for nearly everyone. Applying it:

- **N1 $10 — the fixed-cost control rung.** 0.35% of the median pool and 0.63%
  of the thinnest one, so impact should be negligible everywhere and what
  remains is the size-**independent** cost floor: network fee, any flat venue
  fee, any account-creation cost the venue quotes. Deliberately below any
  plausible entry. V1 put this rung at $25 — 1.6% of the thinnest pool, where
  impact is no longer negligible and the control is contaminated.
- **N2 $50 — the smallest plausibly-real size.** Spans 3.1% of the thinnest pool
  down to 0.03% of the deepest: two orders of magnitude of ratio across the
  population, which is what non-degeneracy requires.
- **N3 $150 — the discriminating rung, set at ~5% of the MEDIAN observed pool.**
  This is the anchor that replaces the $5,000 constant. 5% of the median is
  large enough that impact must be measurable on a typical token and small
  enough that it is still a trade someone could plausibly make. Across the
  population it ranges 9.4% (p0) to 0.09% (p100). **If any single rung decides
  this milestone, it is this one.**
- **N4 $500 — the bounded top rung.** Bounded deliberately: at 17% of the median
  and 31% of the thinnest observed pool, it is aggressive but still a quote, not
  a pool-draining hypothetical. It is also the only rung that says anything
  about the deep tail, where it is a mere 4.3% (p75) and 0.75% (p95). V1's
  $2,000 exceeded the thinnest pool's entire TVL; **no V2 rung exceeds ~31% of
  even the thinnest observed pool.**

**What is still explicitly NOT claimed:** that these are good position sizes,
that we intend to trade them, or that they were derived from any signal,
conviction, or capital base. They are fixed constants of the measurement
instrument, now calibrated to the depth of the thing being measured — the same
sense in which you choose a probe size to match the circuit. No code reads them
to decide anything, and `docs/SAFETY_BOUNDARIES.md` keeps portfolio sizing
forbidden: "a size is a stated INPUT of the simulation… it is not a sizing
recommendation, and nothing may derive, optimize, rank, or recommend a size from
a modeled result."

**A note on what the ratios are and are not.** Every percentage above is a
notional-to-TVL *ratio*, not a predicted price impact. Converting one to the
other requires a curve model, and this milestone deliberately does not carry one
— impact is OBSERVED from the quote response (§5.3) or it is a typed absence.
The ratios are used only to choose probe sizes, which is a question about the
instrument, not about the answer.

### 4.2.1 The measurement's limits, and what would make me revisit the ladder

Recorded honestly, because a ladder calibrated on a weak sample is still a
ladder calibrated on a sample:

- **n = 42**, drawn from roughly the first three hours of a lane activated on
  2026-08-15. This is a small sample and a short window.
- **It is a distribution of OBSERVED pools, not of all births.** A token only
  enters it if the provider returned a usable, identity-matching pair with a
  liquidity state at observation time. Tokens whose pools died, never carried a
  liquidity state, or failed identity are absent. **That bias most likely runs
  UPWARD** — the observed sample is probably deeper than the true birth
  population — which argues for rungs at or below these values, not above them.
  Sample growth does **not** remove this bias; it is structural.
- Nothing here says the distribution is stable. Memecoin liquidity regimes shift.

**What would make me revisit — and the cost of waiting.** The honest statement
is that revisiting has a hard deadline: **the ladder must freeze before CP-1**,
and CP-1 gates every checkpoint after it. Waiting for n to grow delays the whole
milestone while the 24h observations mature without a quote lane, and it does
not fix the observed-pools bias. More importantly, once quoting begins there is
no legitimate revisit at all: a ladder re-anchored on data that includes quote
results is not a preregistration, it is a narrative.

So the pre-committed rule is asymmetric on purpose:

- **Before CP-1** the rungs may still be re-anchored, on Eric's decision, on
  grounds containing no quote result (§4.5).
- **After the first quote, the ladder does not move.** If the realized
  distribution over the CP-7 window turns out to have a median outside roughly
  [0.5x, 2x] of $2,860, that mismatch is **reported as a stated limitation of
  the result** — the instrument is not retuned to flatter the measurement.

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
ladder_id      : "SRO001-LADDER-V2"
rungs_usd      : [10, 50, 150, 500]
entry_mint     : <verified at CP-0>
entry_decimals : <verified at CP-0>
rungs_base_units : [<computed once at CP-1>]
direction_rule : "exit input = entry amount_out, same rung, same pass"
population_rule: "all sparse-lane observations in pass, ascending observation id, capped"
ladder_digest  : sha256 over the canonical encoding of the above
```

### 4.5.1 The amendment record — V1 superseded by V2

The one amendment window described below has now been **used**. Nothing was
edited in place; both versions stand on the record.

| | V1 | V2 |
|---|---|---|
| `ladder_id` | `SRO001-LADDER-V1` | `SRO001-LADDER-V2` |
| `rungs_usd` | `[25, 100, 500, 2000]` | `[10, 50, 150, 500]` |
| anchor | `crypto_min_liquidity_usd = 5000.0` and the $1,000,000 quality-score saturation point | the measured median observed pool ($2,860), n=42, cohort 8 |
| status | **SUPERSEDED, never used to request a quote** | **PROPOSED — freezes at CP-1 on Eric's approval (Q-B)** |
| `ladder_digest` | computed and recorded at CP-1 alongside V2, so the supersession is auditable | computed at CP-1 |

**Reason for the amendment, stated so it can be judged:** V1's anchors were two
repository constants that the measured distribution shows are in the wrong
place for this population — 62% of it sits below the $5,000 floor, and nothing
comes within 6x of the $1,000,000 saturation point. V1's top rung was 70% of the
median pool and 126% of the thinnest, which is a block trade rather than a
quote (§4.2).

**This amendment is legitimate under the rule below for one specific reason: it
contains no quote result.** A liquidity distribution is not an execution
outcome. No quote has been requested by anyone for this milestone, so there is
nothing about favourable execution to have optimized toward. That is exactly the
case the window was written for — and it is now spent.

**The window is closed.** Any further change to the rungs requires a new,
explicit decision from Eric, recorded here as a V3 with its reason, and it
remains impossible after the first quote regardless of who asks.

### 4.5.2 The freeze

**These notionals are FROZEN. Changing them after any quote has been evaluated
invalidates the measurement**, and there is no version of that change that
merely "improves" it: a ladder chosen after seeing which rungs produced
favourable execution is not a measurement of execution cost, it is a report of
which sizes happened to look good, and no one reading the results afterwards can
tell the two apart.

Enforcement, not intention:

- `ladder_digest` is written on **every persisted quote row** (§5). SC-6 fails
  the milestone if any row disagrees with the preregistered value.
- The final report prints the ladder digest next to the verdict. A verdict whose
  rows carry more than one ladder digest is **reported as invalid**, not
  reconciled.
- **One amendment window existed, between CP-0 and CP-1, requiring Eric's
  explicit approval and grounds containing no quote result. It has been used
  (§4.5.1) and does not reopen.** After the first quote request is issued the
  ladder is frozen absolutely.


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

---

## 10. Risks and validation plan

### 10.1 Risks, ranked

| rank | risk | mitigation |
|---|---|---|
| **R1 — HIGH** | **The quote becomes a fill.** An observed `amount_out` is read two milestones later as what we would have received, its provenance evaporates, and a modeled P&L wears observed clothes. | Structural: no fill/order/position/P&L column exists (§11); the `PAPER_SIMULATION` model-id and basis requirement travels *on the artifact*. Process: CP-5's boundary reviewer is charged with hunting consumers. **Honest note: the structural half only covers this repository's own schema. The process half is the weakest link in the design, exactly as it was in the scope doc.** |
| **R2 — HIGH** | **Boundary creep to the sibling route.** "We already have the quote; the build endpoint is one call away." | The amendment names this case explicitly. Structurally: one route constant, no path parameter (§7.4); forbidden-response refusal; SC-7; a dedicated CP-5 reviewer charge. The AST audit's `swap` ban stays in force everywhere except the one narrowly unbanned fragment (§12 FU-2). |
| **R3 — HIGH** | **Silent decimal truncation under a valid digest.** Already observed in this repository on ordinary venue JSON (KALSHI-ARCHIVE-REPLAY-INTEGRITY-001). Base-unit amounts are exactly the shape that triggers it. | Reuse `app/realtime/canonical.py`; no float in the parse path or the digest preimage; an explicit CP-2 test on a value that truncates under `float`. |
| **R4 — MED** | **The ladder drifts.** Rungs get "tuned" once results are in, and the preregistration silently becomes a narrative. | `ladder_digest` on every row; SC-6; the report prints the digest next to the verdict; multi-digest windows are reported invalid rather than reconciled; the one amendment window closes at the first quote (§4.5). |
| **R5 — MED** | **Cherry-picking moves up a level** — the notionals are frozen but the token population is not. | §4.4 preregisters the population, the deterministic order, and the cap; truncation is typed. |
| **R6 — MED** | **Database growth** on a database already past its 3,072 MiB gate: 8 rows per observation is a real multiplier. | Small declared per-pass cap; no raw body persisted; bounded route column with a typed `route_too_large`; cap set from the measured headroom (§14, M3), not from this document's arithmetic. |
| **R7 — MED** | **Rate limiting or an unannounced free-tier change** turns most of the window into failures. | Failures are first-class rows (§5.1), so the window still produces a measurement — of quote availability, which is a legitimate answer to the milestone's question. Cap sized against the published limit at CP-0. |
| **R8 — MED** | **The endpoint requires an account binding**, and someone supplies a "harmless" placeholder address to get it working. | §3.3 forbids all four variants by name; the typed outcome `quote_requires_account_binding` exists precisely so the honest path is the easy path; CP-5 boundary reviewer checks `request_params` in the digest preimage. |
| **R9 — MED** | **Token-2022 transfer fees / hooks silently invalidate every quote** (§8.1, 5d). | Recorded as an absence on **every** row, permanently, rather than omitted. An unknown recorded on every row is honest; an unknown omitted is not. It also bounds the strongest verdict this milestone can reach. |
| **R10 — LOW** | **Write-lock contention** on the shared SQLite host. | Quote rows are written inside the sparse lane's existing batched write phase, never in their own transaction and never in a phase without a session; proven at CP-4 by commit-count equality. |
| **R11 — LOW** | **A stale quote read as fresh.** | §7.2 typed `stale_context`; `freshness_basis` records when only the weaker latency check was available. |

### 10.2 Validation plan

| checkpoint | proven by |
|---|---|
| **CP-0** | committed fixtures; G1/G2 evidence attached; the five reconnaissance answers written down with the evidence, not asserted; the terminate/continue decision recorded in this document |
| **GATE-FU2** | the allowlist diff scoped to one fragment in one file, recorded in `docs/SAFETY_BOUNDARIES.md`, with dual confirmation; `frontier-eval-report --include-safety` clean afterwards **and** demonstrably still failing on a deliberately-added second use outside that file |
| **CP-1** | `alembic upgrade`/`downgrade` round trip; flag-off no-op test; an import-time invariant test that checks the `raise` fires (asserts are stripped under `python -O`); the ladder digest written into §4.5 and matching a test constant |
| **CP-2** | one named test per fabrication shape; the absence/NULL biconditional in both directions; the float-truncation test; direction-aware identity including the quote-side rejection; digest recomputation equality; the forbidden-response whole-refusal test; a purity test that the derivation takes no session, no socket, and no clock |
| **CP-3** | fixture-only tests; a signature test that no path/endpoint argument exists; a policy test that a paid provider is denied before any client or socket is constructed |
| **CP-4** | flag-off byte-identical pass result vs `main`; `external_calls` equality; commit-count equality; full suite green (the **whole** suite, not `-k` a keyword — a filtered run has been mistaken for a full one in this repository before) |
| **CP-5** | three independent PASS verdicts, each recorded separately; the `AGENTS.md` safety grep clean; `frontier-eval-report --include-safety` clean |
| **CP-6** | on-host with the flag OFF: one pass, result identical to pre-deploy, zero rows in the new table, zero external calls attributable to the lane |
| **CP-7** | the report computed from persisted rows only, emitting one of the three verdicts, with SC-1..SC-7 each evaluated explicitly and each reported pass/fail — including SC-6's ladder-digest equality and SC-7's route/response audit |

**A known weakness, stated rather than assumed away.** SC-1 is checkable after
the fact only if the per-pass request ledger is available. The sparse lane
reports its ledger in the pass result but does not persist it. Two options,
neither free: evaluate SC-1 **live** during the window from the pass result (no
schema change, but SC-1 becomes unverifiable afterwards), or persist a per-pass
run row (a second table, more growth on a constrained database). The default
recommendation is the live evaluation, with the loss of after-the-fact
verifiability recorded as a limitation of the result. **This is Q-D for Eric.**

---

## 11. Non-goals — explicit

- **No `PaperFill`.** None, in any form. Not a row, not a table, not a column,
  not a field, not a nullable placeholder, not a dataclass, not a "we'll need
  this later" stub.
- **No ledger rows of any kind**: no `PaperOrder`, `Position`, `ExitDecision`,
  `RealizedPaperPnL`, and no `ExecutionQuote` *ledger* row either. This milestone
  produces **route-quote evidence**, which is the input a future `ExecutionQuote`
  would be built from — not the ledger node itself.
- **No downstream scaffolding.** No consumer, no adapter to a future consumer,
  no interface a future consumer would implement, no migration that "reserves"
  a table name.
- **This is a constraint from `docs/SAFETY_BOUNDARIES.md`, not a preference.**
  Under "What 'no implementation surface' means": *"No functions, fields,
  tables, endpoints, or CLI commands for these capabilities — including
  'disabled' or 'placeholder' versions."* A helpfully-disabled `PaperFill` is
  exactly the artifact that clause forbids, and helpfulness is not an exception
  to it.
- **No modeled fill or modeled P&L in this milestone at all.** `PAPER_SIMULATION`
  is permitted by the amendment and is **not exercised here**; MVP-005B still
  governs whether such a lane is built. Nothing in this milestone produces a
  number that would need a model identifier, because it produces no modeled
  numbers.
- **No EV, side, size recommendation, dollar P&L, profit, action, order,
  recommendation, or trade direction** — no such column exists by construction.
  The notionals (§4) are inputs of the measurement instrument, and nothing reads
  them to decide anything.
- **No wallet, no key, no signing, no transaction construction, no
  `simulateTransaction`, no submission, no capital** (§3.2).
- **No paid provider, no paid RPC, no paid trade/orderflow feed, no
  SolanaTracker**, and no request that consumes a paid budget (§3.2 F9).
- **No new provider beyond the single free public quote endpoint.** In
  particular no RPC provider, not even for a slot or a fee.
- **No systemd unit, timer, daemon, or scheduled path installed by this
  milestone.** The lane rides the sparse lane's existing pass.
- **No change** to the frozen-cohort lane, to `DexScreenerAdapter`, to MarketOps
  behaviour, to any retention window, to any pragma, or to any existing
  migration.
- **No historical backfill**, no mass scheduling, no canary cohort, no arming.
- **No edit to `app/services/frontier_eval.py`** by this milestone (§12 FU-2 is
  a separate, dual-confirmed change and is not part of any checkpoint's diff).

---

## 12. Required follow-ups outside this milestone

Both are **separate, explicitly-reviewed changes**. Neither may be folded into
an implementation checkpoint, and neither is done by this document.

### FU-1 — Canon must stop saying this milestone does not exist. (Tier 2)

`app/canon.py:146-172` states that `READ_ONLY_ROUTE_QUOTE` "requires a
separately accepted milestone that **does not yet exist**", and the canon
summary repeats that the amendment "authorizes NO milestone". Eric's B4
instruction (§0.1) accepted this milestone, so that text is now false.
`app/canon.py` and `AGENTS.md` must be updated to name
SOLANA-ROUTE-OBSERVATION-001 as the accepted milestone for this mode. Leaving
canon stale is how the next agent concludes, correctly per canon and wrongly per
reality, that it must stop and report back.

### FU-2 — The AST safety audit will fail, and must be unbanned narrowly. (Tier 3)

`app/services/frontier_eval.py` carries `BANNED_IDENTIFIER_FRAGMENTS`, enforced
by an AST identifier scan over every `.py` file in `app/`. That list contains
**`swap`** and **`jupiter`**, and also `paper_trad`, `expected_value`,
`position_siz`, `portfolio`, `place_order`, `submit_order`, `create_order`. The
canonical text grep in `AGENTS.md` / `docs/TESTING_POLICY.md` likewise matches
`paper_trad` and `wallet`.

**So any implementation of this milestone that names its venue or route in the
obvious way will FAIL the safety audit.** The amendment says so plainly and says
what to do about it:

> "The audit is a **separate enforcement mechanism** from this document and is
> **not amended by it**. Removing a fragment from that list, or adding an
> allowlist entry, weakens an automated control that currently protects every
> file in `app/`; that is its own narrow, separately reviewed change, made when
> an implementation actually needs it, scoped to the exact fragment in the exact
> file (the allowlist exempts a FRAGMENT in a FILE, never a whole file), and
> recorded here. Until then the correct outcome of writing such code is a
> failing safety audit, and the correct response is to open that separate
> change — never to rename an identifier to slip past the scan, and never to
> broaden the allowlist past the one file that needs it."

Consequences for this plan, stated so nobody discovers them at CP-2:

- **Do not touch `app/services/frontier_eval.py` as part of this milestone.**
  This document does not, and no checkpoint does.
- The unban is **GATE-FU2** in §9 — Tier 3, dual confirmation, between CP-0 and
  CP-2 — and must be recorded in `docs/SAFETY_BOUNDARIES.md`.
- It must be scoped to **one fragment in one file**. If the implementation needs
  two files exempted, that is a signal the implementation is spread too wide,
  not a reason to widen the allowlist.
- **Renaming to evade the scan is explicitly forbidden**, and a reviewer should
  treat a suspiciously-euphemistic module name as evidence of exactly that.
- Its validation includes proving the ban still bites: a deliberately-added
  second use outside the exempted file must still fail the audit.

---

## 13. Open questions for Eric

Ordered by how much they block. The scope doc's Q1, Q3, Q4 and Q6 are **closed**
(§0.3) and are not repeated.

**Q-A — TIER 2, and it blocks CP-0.** *Do you consider G1 and G2 met?* Your
instruction conditioned the start on "6h mechanics demonstrably healthy" and
"24h jobs being scheduled correctly". This agent did not contact EVO and
therefore cannot assert either. §14 M1/M2 name the exact checks. **I will not
self-certify your entry gates.**

**Q-B — TIER 2, and it blocks CP-1 (the ladder freeze).** *Do you accept the
ladder $25 / $100 / $500 / $2,000, denominated in stablecoin base units?* The
justification (§4.2) rests on two constants already in this repository —
`crypto_min_liquidity_usd = 5000.0` and the $1,000,000 quality-score saturation
point — and **not** on a measured liquidity distribution, because measuring one
would have required contacting EVO. If you want the rungs anchored on the
observed distribution instead, that measurement (§14, M6) must happen before
CP-1, since after the first quote the ladder is frozen absolutely.

**Q-C — TIER 2, request-budget and interpretation.** *One entry mint, or two?*
The design declares a single stablecoin entry mint so each rung is an exact
integer that cannot drift with the SOL price (§4.3). The cost: many memecoin
routes are SOL-quoted, so a stablecoin-in route may carry an extra hop that a
real entry would not. That bias is **conservative** (an extra hop can only cost
more) and is visible in `route_hops`. Adding a parallel SOL-denominated ladder
would remove the bias but **doubles the request count** and reintroduces
price-driven notional drift. My recommendation is one mint plus the recorded hop
count; the call is yours.

**Q-D — TIER 2, verifiability vs growth.** *Evaluate SC-1 live, or persist a
per-pass ledger?* §10.2 states the tradeoff. Live evaluation costs no schema and
makes SC-1 unverifiable after the fact; persisting costs a second table on a
database already past its growth gate. I lean live, and I want the weakening
recorded as a limitation of the result rather than waved off.

**Q-E — TIER 3, and it blocks CP-2.** *Do you authorize GATE-FU2, the narrow AST
safety-audit unban?* It weakens an automated control that today protects every
file in `app/`. It is dual-confirmation by design. Without it, the correct state
of the repository is a failing safety audit, and the implementation cannot land.

**Q-F — TIER 2, scope discipline.** *If CP-0 finds no free unauthenticated quote
endpoint, do we stop at `quote_unobtainable_free` (my recommendation), or open
the fallback proxy milestone (§8.4)?* CP-0 is designed so stopping is a clean,
unembarrassing outcome. I want the fallback to be a **separately named**
milestone if it happens, so a proxy result is never later read as this
milestone's success.

**Q-G — TIER 2, sequencing.** *Should the CP-7 activation window overlap the
first 24h sparse observations maturing, or follow them?* Your instruction says
"begin this milestone immediately while the first 24h observations mature",
which I read as authorizing the design and build to proceed in parallel — not as
authorizing the live window to open before CP-0..CP-6 pass. Confirming that
reading matters, because it is the difference between parallel work and a
shortcut.

---

## 14. Pending measurements and assumptions — nothing below is established

Collected in one place so none of it is mistaken for a finding. **This agent
made no measurement on EVO-X2 and no provider call of any kind.** Each item
names the check that would settle it; each is a request to Eric or to the CP-0
implementer, not a claim.

| id | item | status | how to settle it |
|---|---|---|---|
| **M1** | 6h sparse-lane mechanics are healthy (Eric's gate G1) | **PENDING MEASUREMENT** | on EVO: recent sparse-pass results — statuses, per-pass `external_calls`, the 6h observed/miss split, and zero provider-policy violations |
| **M2** | 24h jobs are being scheduled correctly (gate G2) | **PENDING MEASUREMENT** | on EVO: 24h member-horizons planned vs bands opened vs observed; that no band closed unobserved for a schedulable reason |
| **M3** | Current database size and headroom against the 3,072 MiB gate | **PENDING MEASUREMENT** | on EVO: DB size now and the recent daily growth rate; the per-pass cap (§5.7) is set from this, not from this document |
| **M4** | Whether the candidate free public quote endpoint requires a key or an account/user parameter | **UNVERIFIED — no call was made** | CP-0 item 1. If it does, the milestone terminates with `quote_unobtainable_free` (§3.3) |
| **M5** | The declared entry mint's address and decimal count | **UNVERIFIED — not recorded anywhere in this repository** | CP-0 item 4, from a free public source. The ladder integers (§4.3) cannot be computed until this is settled |
| **M6** | The liquidity distribution of the population the sparse lane is actually observing | **PENDING MEASUREMENT** | on EVO: the distribution of observed pool liquidity across sparse-lane observations. Needed only if Q-B says the ladder should be anchored on observed data instead of the repo's own constants |
| **M7** | Which of the §5.3 fields the quote response actually carries | **UNVERIFIED** | CP-0 item 2, as committed fixtures |
| **M8** | The endpoint's published rate limit, and its compatibility with the per-pass cap | **UNVERIFIED. No figure is quoted anywhere in this document** | CP-0 item 3 |
| **M9** | Whether any free source exposes token-2022 transfer-fee / transfer-hook state | **UNVERIFIED** | CP-0, as a one-line check against a provider already integrated. If not, the absence is recorded on **every** row, permanently, and it bounds the strongest verdict CP-7 can reach |
| **M10** | Solana base fee, priority fee, associated-token-account rent | **NOT VERIFIED, AND DELIBERATELY NOT QUOTED.** Fetching a priority fee is forbidden (§3.2 F6) | out of scope; recorded so its absence is not mistaken for an oversight |

**No pricing figure, no rate limit, no latency, no row count, and no liquidity
statistic appears as a measured value anywhere in this document.** Where a
number appears, it is either a repository constant with a file-and-line citation
or a declared design choice.
