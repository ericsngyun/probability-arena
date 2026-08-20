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
