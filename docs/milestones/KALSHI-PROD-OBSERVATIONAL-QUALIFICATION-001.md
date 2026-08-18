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

---

## PRE-CAPTURE STATUS — 2026-08-17 (`KALSHI-PROD-QUAL-PRECAPTURE`)

**CAPTURE IS BLOCKED, and nothing here attempted one.** No production
connection, no capture, no credential read. The blocker is operational and
belongs to an operator: `KALSHI_PRIVATE_KEY_PATH=` is set-but-empty on EVO and
the only key present is the DEMO observer credential, so the authenticated
production handshake cannot be attempted at all.

What now exists, so that capture is a short, well-guarded step when the
credential arrives:

| | artifact | state |
|---|---|---|
| structural order-API guard | `scripts/kalshi_prod_observation_guard.py` | CLEAN on `main`'s closure; red state demonstrated 13 ways |
| one archive root per session (§11 B4) | `app/realtime/session_root.py` | refusal proven; `RECORD_SCHEMA_VERSION` untouched |
| the pre-capture gate | `scripts/kalshi_prod_precapture_preflight.py` | guard → endpoint → session root, in that order |
| the endpoint disagreement | `docs/KALSHI_PRODUCTION_ENDPOINT_001.md` | **RESOLVED** — the AsyncAPI spec names `external-api-ws.kalshi.com` and the collector already dials it; `settings.kalshi_ws_url` has no reader, so the `.env` value cannot mis-route a capture |
| tests | `tests/test_kalshi_prod_qual_precapture_001.py` | **46 passed** (measured on `main`) |

**Run this before capture:**

```bash
python scripts/kalshi_prod_precapture_preflight.py --archive-root <ROOT>
```

It exits non-zero unless the order-API guard is clean and the archive root is
free for this session. It opens no socket and reads no credential; the capture
command is a separate, separately-authorized step.

**Prerequisite 4 is still open, and is still the only thing blocking.** The
production WS host remains **UNVERIFIED**: the official Kalshi AsyncAPI spec
names `wss://external-api-ws.kalshi.com/trade-api/ws/v2` and the collector
already uses it, but documentation is not a handshake. §11 B1 closes on the
first successful production connection and not before — the preflight reports
`verified_on_the_wire: false` for exactly that reason.

**§11 B4 is closed by run rule, not by schema.** The contract's text is
unchanged and its characterization test still pins the record schema; what
changed is that the run rule is now enforced by a typed refusal instead of
remembered. A P4 run that wants a single multi-session archive still needs a
`RECORD_SCHEMA_VERSION` bump, and that decision is still outside this
milestone's authority.
