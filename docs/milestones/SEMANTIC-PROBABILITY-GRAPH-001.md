# SEMANTIC-PROBABILITY-GRAPH-001

**Status: DESIGN, NOT IMPLEMENTED.** Agents map markets to canonical
propositions; deterministic math enforces probability constraints. **No agent
ever selects an action.** Nothing here is built or authorized.

The citation-verification section is kept first, because what it found
constrains what the rest of this document is allowed to assume.

**Doctrine 9 applies to this section.** "The paper exists and is about this
topic" is a different claim from "this figure is correct". Each row below says
which one it is.

## Retrieval provenance

| | |
|---|---|
| tool | `WebFetch` (fetches the URL, then a **small model** answers a prompt against the rendered page) |
| what that means | the abstract text below is **one indirection from primary source** — a model's rendering of the page, not raw HTML I inspected |
| date | 2026-08-16 |
| full texts | **NOT read.** Abstracts only. |
| independence | both papers share first author **Jonas Gebele**. They are **not independent** sources. |

## Paper 1 — semantic non-fungibility

- **URL retrieved:** `https://huggingface.co/papers/2601.01706` (the HF mirror, as Eric supplied it). `arxiv.org/abs/2601.01706` was **not** fetched.
- **Title as retrieved:** *Semantic Non-Fungibility and Violations of the Law of One Price in Prediction Markets*
- **Authors as retrieved:** "Jonas Gebele, Matthes" — **the author list came back malformed** (a bare surname, no initial). Treat authorship as low-fidelity; presumably Florian Matthes, per Paper 2.
- **What the abstract actually claims:** prediction markets are fragmented across operator-run platforms and on-chain protocols that independently list economically identical events; with no shared notion of event identity, liquidity does not pool, arbitrage is capital-intensive or unenforceable, and prices violate the Law of One Price. The authors introduce a semantic alignment framework using natural-language descriptions, **resolution semantics**, and temporal scope; they build a human-validated cross-platform dataset of **>100,000 events across ten venues, 2018–2025**; they report that **~6% of events are concurrently listed** across platforms, and that semantically equivalent markets show **persistent execution-aware price deviations of 2–4% on average**, even in liquid, information-rich settings, **driven by structural frictions rather than informational disagreement**.
- **Eric's relayed figure (~2–4% execution-aware gap):** **CONFIRMED PRESENT IN THE ABSTRACT**, with its qualifiers intact — it is an *average*, it is *execution-aware*, it is *among semantically equivalent markets*, and the paper attributes it to frictions.
- **Not relayed, and material:** the **~6% cross-listing rate** is a hard feasibility ceiling on any cross-venue lane, and the framework's own alignment axis is **resolution semantics**, not title paraphrase.

## Paper 2 — executable arbitrage

- **URL retrieved:** `https://arxiv.org/abs/2608.00666`
- **Title as retrieved:** *Executable Arbitrage and Market Efficiency in Prediction Markets*
- **Authors as retrieved:** Jonas Gebele, Timm Mutzel, Florian Matthes
- **What the abstract actually claims:** it distinguishes **payoff-space no-arbitrage** (implied by terminal payoffs) from **protocol-executable no-arbitrage** (which depends on the position transformations a trader can actually perform). Polymarket's negative-risk markets make the distinction observable because the **NegRisk Adapter operationalizes only the NO→YES direction before settlement**. Using depth-aware executable portfolio values plus actor-level transaction histories and on-chain conversion traces, they estimate **$1.12M arbitrage profit** across two channels: **$1.086M converter-enabled** and **$32K settlement-based basket formation**. In the CLOB sample, positive violations **concentrate on the unsupported YES side**, while adapter-supported NO-side violations are less frequent and shorter-lived. They conclude efficiency depends on whether protocols **expose payoff equivalences as executable primitives**, and prototype a bidirectional adapter extension.
- **Eric's relayed $1.12M:** **CONFIRMED PRESENT IN THE ABSTRACT.**
- **Eric's relayed theoretical-vs-protocol-executable distinction:** **CONFIRMED** — it is the paper's central framing, not an incidental remark.
- **Not relayed, and the most important number in either paper for us:** the **$1.086M / $32K split**. The channel that requires a venue-supported pre-settlement transformation captured **~97%** of realised profit; the hold-to-settlement basket route captured **~2.9%**. Kalshi offers us no cross-market merge primitive, so the settlement route is the only one available to us — the one worth 2.9% in the studied sample.

## What the abstracts do NOT support

Absent from both abstracts, therefore **unverified**: sample construction and selection; whether "execution-aware" nets fees, half-spread, capital lock-up, or all three (this ambiguity alone makes the 2–4% unusable as a design input); the human-validation protocol and inter-rater agreement; any error bars, confidence intervals, or robustness checks; venue list; peer-review status; replication. Neither number has been reproduced in-repo, and neither venue-behaviour claim has been checked against wire evidence.

**Load-bearing consequence:** the executable-vs-theoretical *distinction* stands on its own logic and needs neither paper to be true. The *magnitudes* (2–4%, $1.12M) are third-party claims about other people's venues and must not be treated as effect sizes for our own prior.

---

## 1. What the verification actually changed

Three findings from retrieval matter more than the two figures that were relayed.

**The two papers are not independent.** They share a first author. Treat them as
one research programme, not as corroboration.

**The $1.12M splits in a way that nearly deletes the opportunity for us.**
`$1.086M` was **converter-enabled** — it depended on Polymarket's NegRisk
Adapter operationalizing a NO→YES transformation *before settlement*. Only
**$32K (~2.9%)** came from the **hold-to-settlement basket** route. Kalshi
exposes **no cross-market merge primitive**, so the settlement route is the only
one available to us. The headline number is real and describes a mechanism we do
not have.

**Violations concentrate on the side that cannot be executed.** This is the
paper's own finding and it is the most important sentence for this design.
Mispricings persist *precisely where the venue forbids the transformation*. An
efficient market can leave a payoff identity violated indefinitely if nobody can
act on it — so a large observed gap is **weak evidence of opportunity and strong
evidence that the transformation is blocked.** The naive reading of a big number
is inverted.

Also: **~6% of events are cross-listed at all**, a hard ceiling on any
cross-venue lane before any edge question is asked. And the **2–4% figure is not
usable as a design input** — the abstracts never say whether "execution-aware"
nets fees, half-spread, capital lock-up, or all three, and a number whose cost
basis is unknown cannot be compared against our cost floor. Comparing it anyway
is the error doctrine 2 exists to prevent.

**Net effect.** The *distinction* between mathematical and executable arbitrage
(doctrine 11) is confirmed and is the paper's central framing. The *magnitudes*
are third-party claims about a different venue with a primitive we lack. They
motivate; they do not license.

## 2. Nodes are propositions, not tickers

A node is a **canonical proposition**, identified by a digest over its
resolution semantics:

| field | why it is in the digest |
|---|---|
| `resolution_criteria` | the actual test that decides the outcome |
| `resolution_source` | who adjudicates — two markets on "the winner" can differ on the arbiter |
| `resolution_timing` | when it is decided, anchored on `occurrence_datetime` (L23) |
| `scope_qualifiers` | exclusions, tie handling, void conditions, partial settlement |

**Titles are never compared.** Two markets are equivalent only if their
resolution semantics are. Paraphrase similarity is precisely the signal that
produces confident false equivalences, which §5 shows is the failure mode the
rest of the pipeline cannot catch.

**`NOT_EXTRACTED` or `NOT_STATED` in any digest field BLOCKS equivalence**
rather than defaulting to a match (doctrine 10). Unknown is not equal.

**Markets attach as expiring bindings.** Per L23 a ticker's identity does not
persist — `KXMLBGAME-26AUG19…` is gone within a day. A `MarketBinding` therefore
carries the rules-text hash plus a validity window, and **a rules amendment
marks every edge touching it `STALE`**, never "still valid". An edge is a claim
about two rule sets, so it dies when either changes.

## 3. The relationship ontology (closed vocabulary)

| relation | constraint |
|---|---|
| `equivalent` | `P(A) = P(B)` — requires resolution-digest equality |
| `complement` | `P(A) + P(B) = 1` |
| `implication` | `P(A) ≤ P(B)` — direction is load-bearing and easy to invert |
| `mutually_exclusive` | `ΣP ≤ 1` |
| `exhaustive` | `ΣP ≥ 1` |
| `conditional` | `P(A∧B) = P(A\|B)·P(B)` |
| `parent_child` | monotone CDF over parsed strikes ("over 3.5" vs "over 4.5") |
| `cross_venue_equivalent` | `\|P₁ − P₂\| ≤ δ`, **δ typed `UNKNOWN`, never 0** |

`δ` is not zero because venues differ in fees, settlement timing, void rules and
adjudication source. A cross-venue edge asserting exact equality is asserting
those are identical, which is false by default.

**Deliberately excluded: `independent`, `correlated`, `similar`, `related`.**
"Similar" is the relation that kills you: it carries no constraint, so it can
never be violated, so it can never be *wrong* — which is exactly how a guess
gets laundered into a graph that otherwise looks rigorous. **Every admitted
relation must be falsifiable by a price.**

## 4. The gate pipeline — ordered cheapest-to-kill

> semantic relationship → payoff identity → book depth → fees → capital lockup
> → venue transformation → **executable edge**

| gate | needs | have it? |
|---|---|---|
| **G1 semantic** | resolution-digest equality, agent-extracted | agent work, unbuilt |
| **G2 payoff identity** | the constraint from §3 | pure math — **yes** |
| **G3 depth** | executable size at the quoted prices | **yes**, from the fabric |
| **G4 fees** | the venue fee schedule | **assumption only** — `kalshi_fee_rate_assumption = 0.07`, unverified against realised fills |
| **G5 capital lockup** | `occurrence_datetime` → annualised return | **yes** (L23) |
| **G6 venue transformation** | does Kalshi permit the required position transformation | **yes — and the answer is usually no** |
| **G7 executable** | all of the above survive simultaneously | — |

### The worked example, carried all the way through

Trump-2028 nomination, two markets quoted `A: 30/31` and `B: 34/35`, where the
graph asserts `complement`:

```
buy A YES @ 31c  +  buy B NO @ 66c   =  97c  for a guaranteed $1
gross                                 =   3.00c
fees (0.07 · P · (1−P), both legs)    =   1.50c + 1.57c  =  3.07c
--------------------------------------------------------------------
net                                   =  −0.07c        DEAD AT G4
```

**It dies at the fee gate, before capital is even considered.** And had it
survived: 3c on 97c locked for ~23 months to resolution is **1.60% annualised** —
which G5 would then kill against any reasonable hurdle. This is doctrine 11 in
one worked case: the payoff identity was real, and there was never a trade.

**G6 is the gate that matters most on Kalshi and is the least familiar.** The
$1.086M route in the literature required a venue primitive that converts
positions pre-settlement. Kalshi has none. So every Kalshi structural trade is a
**hold-to-settlement basket** — the route that was ~2.9% of realised profit
elsewhere. G6 must be evaluated *before* anyone gets excited about a spread, not
after.

**Ordering is a cost decision, not a correctness one.** G2–G6 are cheap and
deterministic; G1 is expensive and fallible. We run the cheap gates first to
discard most candidates — but §5 explains why that ordering also creates the
design's central hazard.

## 5. Agent controls, and the single most likely false positive

**The failure mode: only G1 can be wrong in a way no later gate can detect.**
G2–G7 measure cost. **None of them ever re-checks semantics.** So a false
`equivalent` passes through every downstream gate untouched.

**Worse, the pipeline selects for the error.** On genuinely equivalent markets
the gap is small and usually dies at G4. On markets that merely *look*
equivalent, the price difference **is the market's priced probability of the
divergence the agent asserted away** — so it is large, and large gaps are
exactly what survives the cost gates. **The candidate most likely to reach G7 is
the one built on a wrong G1.**

Concretely, with a bad `implication` edge — "Trump wins the presidency ⇒ Trump is
the Republican nominee":

```
collect 2c   risking 98c on the third-party / independent-run branch
break-even at a 2% exception rate
```

The ontology has silently emitted a probability assertion it claims never to
make: it asserted `P(exception) < 2%`. **A constraint violation is evidence
against the constraint at least as much as it is evidence of mispricing**, and
nothing downstream of G1 can tell those two apart.

### Controls, all aimed at G1

* **An adversarial refuter.** A second agent is given the proposed edge and
  tasked *only* with refuting it — construct a world where both markets resolve
  differently. Its default answer is REFUTED under uncertainty. An edge survives
  only if refutation fails.
* **Asymmetric confidence thresholds.** The bar for asserting an edge is far
  higher than for withdrawing one. Edges are cheap to remove and expensive to be
  wrong about.
* **Human review tiers**, scaled to what the edge would authorise — a
  `cross_venue_equivalent` on an ambiguous adjudication source is not the same
  risk as `parent_child` over parsed numeric strikes.
* **Size-inverse-to-gap.** Deliberately counter-intuitive and it follows from the
  selection effect above: a *larger* surviving gap warrants *more* scepticism,
  not more size.

### Positive control (doctrine 7)

A fixed corpus of pairs the pipeline must classify correctly, containing **both**
known-equivalent and known-NOT-equivalent pairs — the second kind chosen to be
*paraphrase-similar but semantically distinct*, since that is the discriminating
case. If the pipeline cannot fail on those, its passes mean nothing. Run before
any edge is trusted, and re-run whenever the extraction prompt changes, because a
prompt edit is a silent model change.

## 6. What this cannot do, and what would falsify it

**Cannot:**

* trade — it emits graph edges offline, consumed by deterministic code. **No
  agent is in the synchronous market path** (doctrine 12).
* establish that an edge is *profitable*; G7 says only that it is not obviously
  impossible. The house prior of `e_net ≤ 0` still applies afterwards.
* verify the papers' magnitudes. See the citation section.
* see cross-market transformations Kalshi does not expose — G6 is a venue fact,
  not a modelling choice.

**Falsified if:** the refuter never refutes anything (it is decorative); the
positive control's paraphrase-similar negatives are classified as equivalent;
surviving gaps do not shrink after fees and lockup are applied honestly, which
would suggest a cost bug rather than an edge; or observed violations turn out to
cluster on the **executable** side, which would contradict the paper's finding
and mean the whole G6-first framing is wrong.

**Ordering discipline.** This lane is independent of `MARKET-MICROSTRUCTURE-EDGE-001`
and may proceed on its own, but it does not begin implementation until the
activity profile completes and its own G1 controls are built. The most valuable
early output is not an edge — it is a measured answer to *how often Kalshi
permits the transformation at all*, which is cheap to obtain and bounds the
entire lane.
