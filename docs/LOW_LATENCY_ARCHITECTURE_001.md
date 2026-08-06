# LOW-LATENCY-ARCHITECTURE-001 — program architecture and dated roadmap

**Status:** Phase 0 complete. **KALSHI-REALTIME-OBSERVATION-001A implementation
deferred** on two verified blockers, not on effort — see §12.

Program start: **2026-08-06**.

---

## 1. Verified current state

Independently verified 2026-08-06T22:34Z, not taken from the prompt.

| | |
|---|---|
| Mac / origin / EVO-X2 | `65b5847` — all three agree |
| tracked / untracked | clean / one known stray on EVO, left alone |
| Alembic | `0027` |
| MarketOps | 8,186 lifetime runs, **7 non-ok**, last three `ok` with `stage_errors={}` |
| forecasts / scored | 13,662 / **13,662**, 0 duplicate current scores |
| markets | 108,263 |
| market_outcomes | 10,067 |
| market_price_ticks | 605,700 (**8,550 in the last hour** ≈ 2.4/s) |
| crypto tokens / ticks / horizon obs / discovery | 10,983 / 160,521 / 43 / 345,398 |
| database | **4.24 GiB**, `journal_mode=delete`, page 4096, **1,679 MiB freelist** |
| host free space | 88 GiB of 236 GiB |
| backups | healthy, newest `backup-20260806T013038Z` |
| failed units | none; 7 probability timers |
| `database_locked` events | **5** lifetime |
| experiment registry | baseball **collecting**, manifest intact, chain intact (len 2), drift `none`, predicate digest matches, **no result** |
| drafts | 3 (soccer + tennis blocked, baseball's draft retained) |

**Assessment.** The research and governance plane is in good shape and is the
asset worth protecting. The gap is not analytical, it is temporal: the system's
finest-grained market observation is a ~2.4/s polled tick stream, and every
forecast is joined to prices that were never proven executable at the instant
the forecast existed. Nothing here is wrong; it simply cannot answer an
execution question.

Two structural facts constrain everything below. The database is 4.24 GiB with
`journal_mode=delete` and a lifetime lock-event count of **5** — a number this
project has treated as a protected invariant. And a single long-running watcher
already owns the tick-writer role. High-frequency book deltas must not go near
either.

## 2. Existing-system assessment

**Keep and build on:** the prospective experiment registry (immutable manifests,
typed populations, registry-owned results, append-only governance), outcome
synchronisation at 100% coverage, scoring and reliability decomposition, the
provider-governance discipline, backup and lock telemetry.

**Cannot be reused for execution:** the six-minute MarketOps cycle (a
coordination cadence, not a data path), `market_price_ticks` (polled, no venue
sequence, no book depth), and any float-based comparison of probability to
price.

**The honest statement of where we are:** one registered prospective experiment
is collecting on a *probabilistic* hypothesis. There is no validated executable
strategy in either lane, and this document does not create one.

## 3. Target architecture

Five planes, deliberately separable so a failure in one cannot reach another.

```
┌─ RESEARCH & GOVERNANCE ─────────────────────────────────────┐
│ experiment registry · manifests · results · evidence graph  │  EVO-X2
│ agents (advisory only, never in the order path)             │
└──────────────────────────────────────────────────────────────┘
            ▲ evidence, never control
┌─ REAL-TIME MARKET DATA ─────────────────────────────────────┐
│ Kalshi WS · Polymarket WS · Solana observer                 │  co-located
│ heartbeat · reconnect · snapshot/delta reconciliation       │
└──────────────────────────────────────────────────────────────┘
            │ normalized events (append-only archive)
┌─ POINT-IN-TIME FEATURES ────────────────────────────────────┐
│ in-memory book · feature snapshots with full time lineage   │
└──────────────────────────────────────────────────────────────┘
            │
┌─ SHADOW EXECUTION ──────────────────────────────────────────┐
│ order state machine · fill model · fee/slippage · ledger    │
└──────────────────────────────────────────────────────────────┘
            │ (gated, disabled by default, separate credentials)
┌─ BOUNDED LIVE EXECUTION ────────────────────────────────────┐
│ pre-trade risk gateway · kill switch · intent ledger        │
└──────────────────────────────────────────────────────────────┘
```

## 4. Agentic control model

Agents propose, challenge, audit and request. They never decide.

| agent | produces |
|---|---|
| Research Director | milestone proposals with evidence links |
| Hypothesis | draft manifests (never registers) |
| Experiment Registrar | validation reports; registration needs a human |
| Data Quality | gap/divergence/clock findings |
| Market Mapping | cross-venue identity proposals with confidence |
| Model Critic · Leakage Auditor | falsification attempts |
| Execution Critic · Latency Analyst | fill-model and latency-decomposition critiques |
| Risk Auditor · Incident · Postmortem | risk-state evidence, incident records |
| Portfolio Research | composition and concentration analysis |

**The hard rule.** No LLM output may become an order, signed transaction, size,
price, cancellation, risk-limit change or credential action. Agents are not in
the latency-critical path at all — not for performance reasons but because a
non-deterministic component in an order path cannot be replayed, and anything
that cannot be replayed cannot be audited.

## 5. Deterministic hot path

```
market event → normalized event → book/state → point-in-time features
  → versioned forecast → registered decision rule → risk validation
  → order state machine → fill and position ledger
```

Ordinary Python, no model calls, fully replayable: the same archived event
stream must reproduce the same decisions byte-for-byte. That property is the
acceptance test for the whole path, and it is why the archive is append-only.

## 6. Sports lane

### Kalshi first — verified API facts

Checked against the live AsyncAPI spec on 2026-08-06, not assumed:

| | |
|---|---|
| WebSocket | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` |
| **auth** | **required for market data** — see §12 |
| subscribe | `{"id": N, "cmd": "subscribe", "params": {"channels": [...], "market_tickers": [...]}}` |
| channels | `orderbook_delta`, `ticker`, `trade`, `fill`, `market_positions`, `market_lifecycle_v2`, … |
| snapshot | `msg.yes_dollars_fp`, `msg.no_dollars_fp` — arrays of `[price_dollars, contract_count_fp]` |
| delta | `msg.price_dollars`, `msg.delta_fp`, `msg.side` (`yes`/`no`) |
| ordering | `seq` per subscription, `sid` identifies the stream |

REST auth is `KALSHI-ACCESS-KEY` + `KALSHI-ACCESS-TIMESTAMP` (ms) +
`KALSHI-ACCESS-SIGNATURE` = base64(RSA-PSS-SHA256(`timestamp + METHOD + path`)),
path without query.

**Canonical price normalisation.** Kalshi publishes two books — YES and NO — and
they are not a bid/ask pair. A resting NO bid at price *p* is economically a YES
offer at (1 − *p*). The collector must normalise both into one YES-denominated
ladder and **prove** it, because assuming conventional bid/ask semantics here is
the single most likely way to build a plausible, wrong book. This gets a
dedicated property test before any strategy sees it.

### Polymarket second

CLOB V2, token/market mapping, snapshots + incremental updates, tick-size
changes, negative-risk markets. No cross-venue comparison is valid until
resolution semantics, participants, start times and settlement conditions are
proven structurally equivalent — matching on title strings is forbidden.

### Sports identity graph

`sport · league · event · participant · scheduled_start · venue_market ·
contract · outcome · resolution_rule · mapping_confidence`. Mapping confidence
is a first-class field because a wrong cross-venue join produces a *confident*
false arbitrage.

## 7. Crypto lane

The lane must keep six things apart that are routinely conflated:

`signal quality` · `chart-price movement` · `route availability` ·
`execution feasibility` · `transaction landing` · `exit feasibility`

**A chart-price increase is not a simulated profit unless both entry and exit
routes were realistically available.** The existing crypto measurement (10,983
tokens, 345,398 discovery events, 43 horizon observations) covers signal and
price; it covers none of the other four. Observation of pools, routes, priority
fees, compute units, slot/block, and buy/sell route availability comes first.

## 8. Fixed-point calculation contract

**No binary floating point** for money, prices, contract counts, position
values, fees, P&L, slippage, probability-to-price comparison, or Solana token
amounts.

Every operation declares: `input units · scale · rounding direction · rounding
point · fee source · fee version · output units`.

Kalshi's own `*_dollars` / `*_fp` field naming is an invitation to get this
right: prices arrive as decimal dollars and counts as fixed-point integers.
They will be parsed into `Decimal` and integer smallest-units respectively, and
never into `float` — including at JSON parse time, where `json.loads` will be
given `parse_float=Decimal`.

Property tests required before any strategy: complement prices (YES *p* ↔ NO
1−*p*), bid/ask conversion, fractional quantity, fee rounding, partial fills,
realised and unrealised P&L, settlement, zero and boundary quantities, repeated
replay, deterministic serialisation.

## 9. Time and latency contract

UTC everywhere, timezone-aware, monotonic clocks for durations, clock-offset
monitoring with alerts. Every derived feature carries `event_time`,
`venue_time`, `collector_receive_time`, `normalization_time`, `feature_time`,
`forecast_time`, `decision_time`, `data_age_ms`, `source_event_ids`,
`implementation_version`.

Latency is **never** one number. It is decomposed into eight measured hops:
venue→receive, receive→normalized, normalized→feature, feature→decision,
decision→risk, risk→submit, submit→ack, ack→fill. Reporting a single figure
hides exactly the hop that is broken.

## 10. Storage architecture

**Decision: in-memory book + append-only compressed event files + Parquet
research archive + SQLite for low-frequency metadata only.**

Book deltas do **not** go in `probability_arena.db`. That database is 4.24 GiB
on `journal_mode=delete` with 5 lifetime lock events and a long-running watcher
already writing ticks; adding a high-frequency writer would put the research
asset at risk to buy nothing.

No Kafka, Redpanda, NATS, ClickHouse, PostgreSQL or Rust in this program yet. A
bus is justified only by multiple independent consumers needing durable fan-out,
which does not exist today. Python stays until a profiled SLO fails; only then
does a measured hot-path component move to Rust.

## 11. Risk and capability modes

```
OBSERVE_ONLY    market data only; no order imports resolvable
SHADOW_ONLY     + simulated orders; no venue write path exists
DEMO_EXECUTION  + demo credentials, isolated process and key scope
LIVE_BOUNDED    + production credentials, human-activated, hard limits
```

Code **fails closed** when a requested capability exceeds the active mode.
Observe-only and shadow processes must not require production credentials, and
live credentials must be unreachable from research agents. The mode is a process
boundary, not a flag read at call time.

## 12. Why 001A is deferred — two verified blockers

Both were found by doing the verification this milestone demanded. Neither is a
matter of effort.

### Blocker 1 — Kalshi market data requires authentication

The AsyncAPI spec states authentication is required to establish the WebSocket
connection. There is no anonymous market-data feed. That collides directly with
the scope line "no trading credentials beyond read-only requirements": a Kalshi
API key is an **account credential**, and the RSA private key that signs with it
is the same key class that would later authorise orders.

This needs a human decision before any code exists, covering: which account the
key belongs to (a dedicated read-only research account, or the trading account),
where the private key lives (not this repo, not the research database), which
process may read it, and whether an `OBSERVE_ONLY` process holding an
order-capable credential is acceptable at all. My recommendation is a **separate
Kalshi account** provisioned solely for market data, so the observe-only plane
never holds a credential that could place an order — but that is an account and
custody decision, not an implementation one.

### Blocker 2 — the `_fp` scale is undocumented

`contract_count_fp` and `delta_fp` are fixed-point integers whose **divisor is
not stated** in the API keys documentation or the spec excerpt. Building the
fixed-point primitives against a guessed scale would be guessing at precisely
the layer §8 forbids guessing at, and a wrong divisor produces a book that looks
entirely reasonable and is wrong by a constant factor — the hardest class of
error to notice downstream.

Resolution: read the scale from the full spec or confirm it empirically against
a live snapshot whose `*_dollars` values can be cross-checked against the
existing REST `yes_bid`/`yes_ask` for the same market. That empirical check
needs Blocker 1 resolved first.

**Implementing 001A now would mean writing an authenticated client with no
authorised credential and a fixed-point parser with an assumed divisor.** Both
are exactly the kind of plausible-but-unverified work this project has spent
eight milestones learning to refuse.

## 13. Dated roadmap

| phase | window | milestone | status |
|---|---|---|---|
| 0 | Aug 6–13 | `LOW-LATENCY-ARCHITECTURE-001` | **this document** |
| 1 | Aug 7–20 | `KALSHI-REALTIME-OBSERVATION-001A` | **blocked** on §12 |
| 2 | Aug 14–27 | `EXECUTION-MATH-FOUNDATION-001` | needs the `_fp` scale |
| 2b | Aug 14–27 | `SOLANA-ROUTE-OBSERVATION-001` (design + read-only) | unblocked, can start |
| 3 | Aug 21–Sep 10 | `KALSHI-SHADOW-EXECUTION-001` + `POLYMARKET-REALTIME-OBSERVATION-001` | |
| 4 | Sep 11–Oct 1 | `KALSHI-DEMO-EXECUTION-001` | |
| 5 | Sep 18–Oct 15 | `SPORTS-SHADOW-STRATEGY-001` | requires baseball's experiment to reach its conditions |
| 6 | Oct 15–Nov 12 | `KALSHI-LIVE-BOUNDED-CANARY-001` | only with separate human authorisation |

Crypto: Aug observations → Sep prospective signal experiments → Oct shadow
execution → Nov–Dec execution-adjusted evidence → **earliest** bounded live
canary Nov 2026–Jan 2027.

**Interlock worth naming:** Phase 5 depends on the baseball experiment, whose
registered floor is 500 with a `not_after` of 2027-02-02. At the observed
baseball cadence (~410 forecasts/day) the floor is reachable in days, but the
stopping rule forbids evaluating before it is met — so Phase 5's earliest
honest start is governed by the registry, not the calendar.

## 14. Go-live gates

Calendar completion is never sufficient.

**Data integrity** — deterministic replay; no unexplained sequence gaps; no
persistent book divergence; clock health; bounded reconnect recovery; explicit
stale-data rejection; no silent event loss.

**Calculation integrity** — fixed-point throughout; fee parity against venue
examples; fill and settlement reconciliation; deterministic P&L; property tests;
adversarial numerical review.

**Shadow evidence** — ≥30 calendar days **or** a registered decision floor,
whichever is later, reporting fills, partial fills, no-fills, gross result, fees,
slippage, latency cost, adverse selection, max drawdown, concentration and
regime composition.

**Operational integrity** — clean backup *and restore* test; kill-switch test;
credential separation; duplicate-order prevention; restart/reconnect tests;
host-failure recovery; incident runbook; human activation; zero unresolved
critical alerts.

**Strategy integrity** — prospective registration; fixed population, entry/exit
rules, fee/slippage model, sample floor, stopping rule; no post-result cohort
changes; no favourable conclusion below the floor.

**Live authorisation** — a separate explicit approval naming venue, strategy
version, max order size, max gross exposure, max daily loss, max open positions,
activation interval, rollback condition, credential scope. **No standing
authorisation is created by this document.**

## 15. Earliest responsible live dates

| lane | earliest | binding constraint |
|---|---|---|
| Kalshi bounded canary | **mid-November 2026** | 30-day shadow evidence cannot start before Phase 3 completes |
| Solana bounded canary | **January 2027** | execution feasibility, not signal, is the unknown |

Both assume every gate passes on the first attempt, which has not happened once
in this project's history. Treat them as floors, not estimates.

## 16. Execution location

EVO-X2 remains the research, governance, replay and supervisory plane and the
canonical database does **not** move to reduce execution latency.

Before Phase 3, benchmark at least three candidate locations for the collector
and any future executor, measuring WebSocket RTT, REST RTT, order-ack latency,
packet loss, jitter, reconnect behaviour and host clock quality. Choose from
measurement. EVO-X2 is a residential-adjacent host and is unlikely to win, but
that is a hypothesis to test rather than assume.
