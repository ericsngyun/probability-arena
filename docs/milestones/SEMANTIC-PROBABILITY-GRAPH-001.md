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
