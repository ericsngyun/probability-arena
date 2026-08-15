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
