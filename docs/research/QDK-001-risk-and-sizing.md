# QDK-001 — Risk and position-sizing layer (DESIGN-AHEAD RESEARCH ONLY)

> **STATUS: RESEARCH DOCUMENT. NOT A BUILD AUTHORIZATION. NOT A DESIGN THAT MAY BE
> IMPLEMENTED.**
>
> This document designs a layer that **does not exist and is not authorized to be
> built**. Per `docs/SAFETY_BOUNDARIES.md`, as of 2026-08-14:
>
> - **Portfolio sizing** — ❌ no implementation surface. Gate: "post-paper-trading
>   milestone with explicit human acceptance."
> - **EV calculation (dollar EV)** — ❌ "remains forbidden with no unlocking
>   milestone defined."
> - **Trade recommendations** — ❌ no implementation surface.
> - **Order placement / live trading / autonomous trading** — ❌ no implementation
>   surface.
> - **Real capital, real orders, real positions, real fills** — ❌ forbidden under
>   every mode including `PAPER_SIMULATION`.
>
> Every quantity in this document is a **mathematical object in a modeled
> context**. `f` is a dimensionless fraction of a hypothetical modeled bankroll,
> never a dollar amount, never a contract count, never a side, and never an
> instruction. The only place any of this may ever operate is a
> `PAPER_SIMULATION` context that carries an explicit model identifier and a
> modeled-vs-observed basis on every artifact — and that mode is itself
> unimplemented.
>
> **This document also does not authorize its own implementation.** Writing code
> that names these concepts will fail the AST safety audit
> (`BANNED_IDENTIFIER_FRAGMENTS` contains `kelly`, `position_siz`, `portfolio`,
> `expected_value`, `paper_trad`), and per the SAFETY-BOUNDARY-ROUTE-QUOTE-001
> amendment that failure is the **correct** outcome. This file is markdown under
> `docs/research/`; the canonical safety grep is scoped to `app/ --include="*.py"`
> and is unaffected.

## Table of contents

1. [Scope, notation, and what a "size" means here](#1-scope-notation-and-what-a-size-means-here)
2. [Track 1 — Kelly and its failure modes](#2-track-1--kelly-and-its-failure-modes)
3. [Track 2 — Uncertainty-aware sizing](#3-track-2--uncertainty-aware-sizing)
4. [Track 3 — Tail risk: CVaR and EVaR as constraints](#4-track-3--tail-risk-cvar-and-evar-as-constraints)
5. [Track 4 — Drawdown and ruin](#5-track-4--drawdown-and-ruin)
6. [Track 5 — Correlation and concentration](#6-track-5--correlation-and-concentration)
7. [Track 6 — Abstention as a first-class action](#7-track-6--abstention-as-a-first-class-action)
8. [Track 7 — The definition of ready, and the sample-size answer](#8-track-7--the-definition-of-ready-and-the-sample-size-answer)
9. [Challenge to the formula](#9-challenge-to-the-formula)
10. [Consolidated term reference](#10-consolidated-term-reference)
11. [Evidence ledger — VERIFIED vs INFERRED](#11-evidence-ledger--verified-vs-inferred)

## 1. Scope, notation, and what a "size" means here

_(to be filled)_

## 2. Track 1 — Kelly and its failure modes

_(to be filled)_

## 3. Track 2 — Uncertainty-aware sizing

_(to be filled)_

## 4. Track 3 — Tail risk: CVaR and EVaR as constraints

_(to be filled)_

## 5. Track 4 — Drawdown and ruin

_(to be filled)_

## 6. Track 5 — Correlation and concentration

_(to be filled)_

## 7. Track 6 — Abstention as a first-class action

_(to be filled)_

## 8. Track 7 — The definition of ready, and the sample-size answer

_(to be filled)_

## 9. Challenge to the formula

_(to be filled)_

## 10. Consolidated term reference

_(to be filled)_

## 11. Evidence ledger — VERIFIED vs INFERRED

_(to be filled)_
