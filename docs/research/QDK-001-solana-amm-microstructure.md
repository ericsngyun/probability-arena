# QDK-001 — Solana AMM microstructure: the market-state model for memecoins

**Status:** RESEARCH ONLY. No production code, no live execution, no trading, no
provider call was made in the course of writing this document.

**Scope:** establish the correct state model for Solana memecoin markets, which
trade on bonding curves and automated market makers rather than on limit order
books. Establish what transfers from classical microstructure, what must be
discarded, and what is honestly timeable.

**Evidence labels used throughout.** Every substantive claim carries one:

- **VERIFIED** — confirmed against a primary source (protocol documentation,
  on-chain program source, a paper that was fetched and read) or against a
  measurement already recorded in this repository, with a citation.
- **INFERRED** — a deduction from VERIFIED facts, with the deduction stated.
- **SPECULATIVE** — plausible, unconfirmed, and load-bearing for nothing.

---

## Table of contents

1. [The central architectural point: there is no book](#1-the-central-architectural-point-there-is-no-book)
2. [Impact mathematics: constant product, with fees, derived](#2-impact-mathematics-constant-product-with-fees-derived)
3. [Impact mathematics: the pump.fun bonding curve](#3-impact-mathematics-the-pumpfun-bonding-curve)
4. [What is actually uncertain — the real research question](#4-what-is-actually-uncertain--the-real-research-question)
5. [The AMM state vector, as a typed feature schema](#5-the-amm-state-vector-as-a-typed-feature-schema)
6. [Flow as a point process: does Hawkes transfer?](#6-flow-as-a-point-process-does-hawkes-transfer)
7. [Lifecycle as the dominant regime variable](#7-lifecycle-as-the-dominant-regime-variable)
8. [Adverse selection and toxicity without a market maker](#8-adverse-selection-and-toxicity-without-a-market-maker)
9. [What is genuinely timeable — an honest assessment](#9-what-is-genuinely-timeable--an-honest-assessment)
10. [DISCARD list: classical CLOB constructs that do not transfer](#10-discard-list-classical-clob-constructs-that-do-not-transfer)
11. [What our data can and cannot support today](#11-what-our-data-can-and-cannot-support-today)
12. [Bibliography, with verification status](#12-bibliography-with-verification-status)

---

<!-- SECTIONS FILLED IN INCREMENTALLY; each commit adds one -->
