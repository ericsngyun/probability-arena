# KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001

**Status: AUTHORIZED, NOT STARTED.** Scheduled **after** the DEMO qualification
(CP6–CP9), the generation-aware `publishable_books()` fix, and the tape schema
freeze — and **BEFORE** `MARKET-MICROSTRUCTURE-EDGE-001`.

**Read-only production tape. Same collector. No orders, no portfolio channels,
no venue writes, no capital.**

---

## Why this exists

DEMO is increasingly looking like a **functional sandbox, not a realistic
microstructure environment**. `KALSHI-TAPE-MANIFEST-001` measured its activity
distribution as **four hyperactive markets → a 98.3× cliff → a broad quasi-flat
plateau**, with 30.9% of eligible markets inside a single 15–17 c/min band.
That is not a market distribution; it is what simulated flow looks like.

**Without this milestone, `MARKET-MICROSTRUCTURE-EDGE-001` risks building a
sophisticated alpha study over sandbox-generated flow.** Every feature family it
proposes — OFI, queue imbalance, microprice, cancellation intensity, liquidity
regimes, adverse selection — is a claim about *how real order flow behaves*. A
model fitted to synthetic uniform flow would be internally consistent, well
validated, and about nothing.

## What DEMO qualification can and cannot establish

A successful CP6–CP9 run on DEMO **may** establish:

- transport stability
- sequence correctness
- reconnect / generation behaviour
- raw-frame conservation
- replay equality
- archive correctness
- bounded collector lag **under DEMO load**
- segment rotation / close behaviour
- metrics correctness

It **may NOT** establish:

- production message-rate capacity
- production latency tails
- production liquidity regimes
- representative order-flow statistics
- expected production microstructure
- production sizing / capacity

**Those are what this milestone is for.** CP9 must state the distinction on its
face rather than leaving a reader to infer it.

## Scope

Same collector, unchanged, pointed at production market-data channels:

- read-only; market-data channels only, **no private/portfolio channel**
- a frozen manifest with the same reproducibility requirements: stratification
  snapshot timestamp, ranking statistic **with its limitations stated**, full
  candidate population
- the production universe stratified on **empirically measured** regimes, not on
  the regime structure DEMO happened to show
- **field semantics verified before use** (doctrine 8) — production may differ
  from DEMO in exactly the way `updated_time` differed from its name
- the same four-way verdict as CP9: qualified / conditionally qualified /
  underpowered / failed

## Prerequisites

1. CP6–CP9 complete on DEMO (functional qualification)
2. `KALSHI-REPLAY-GENERATION-CONSISTENCY-001` merged
3. Tape schema frozen and reviewed as a **measurement contract**
4. A production read-scoped credential whose scopes are verified against the
   live key-metadata route, and a **confirmed** production WS host —
   `kalshi.py:52-55` currently records it as **UNVERIFIED**

## The comparison that matters

Report explicitly whether **the DEMO rate distribution predicted the production
one**. CP10 was already written to check this; the manifest finding makes it the
central question rather than a footnote. If DEMO does not predict production,
say so plainly — it bears on every rotation constant tuned against DEMO, and on
whether DEMO is useful for anything beyond functional correctness.
