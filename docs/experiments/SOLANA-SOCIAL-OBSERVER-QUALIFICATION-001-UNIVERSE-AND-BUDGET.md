# Source universe and cost envelope — FROZEN

Under `SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001`. Written 2026-08-28, **before
any socket is opened and before any live count exists.**

**This document freezes the design. It does not authorize a run** — see §7 for
the two blockers that make a run impossible today.

---

## 1. Selection basis, and what it forbids

Sources are selected for **protocol, access, authority and message-shape
diversity**. The universe must exercise the paths the funnel has to handle, so
that a zero at any stage means "this did not happen" rather than "that code
never ran".

**Forbidden as selection criteria, without exception:**

* historical profitability, past "good calls", or any return series;
* follower count, engagement, or reputation;
* any belief that a source has alpha.

A universe chosen on past profitability makes the funnel a measurement of our
own hindsight rather than of the pipeline. **No return series, price history or
performance anecdote may be consulted during selection**, and the justification
recorded for each source must cite only protocol/access/shape properties.

## 2. Class quotas — 18 sources, 6 classes

| # | class | n | what it exercises |
|---|---|---:|---|
| 1 | official project accounts that publish contract addresses | 4 | `PUBLISHED_MINT`, `DIRECT`, reciprocal-attestation authority |
| 2 | launchpad / ecosystem announcement accounts | 3 | `LAUNCHPAD_IDENTIFIES_ACCOUNT` authority evidence |
| 3 | exchange / listing announcement accounts | 3 | migration and re-listing shapes, `MIGRATED_MINT` |
| 4 | high-volume community/aggregator accounts | 4 | quotes, forwards, duplication, propagation collapse |
| 5 | accounts that post tickers **without** addresses | 2 | `TICKER_ONLY` — must never resolve identity |
| 6 | known-impersonator / lookalike accounts, if identifiable | 2 | `IMPERSONATOR` authority path, `NAMES_DIFFERENT_ACCOUNT` |

Classes 5 and 6 exist **specifically to produce refusals**. A qualification in
which nothing is refused has not tested the refusal paths.

## 3. CONTROL and NATURAL_LIVE are separate populations

| population | source | counts toward funnel/latency |
|---|---|---|
| `NATURAL_LIVE` | the frozen universe, arriving on the live stream | **yes** |
| `CONTROL` | deliberately injected artifacts of known shape | **no** |

Controls exist so a zero is interpretable — a known-attested source, a known
mint-bearing shape, and a deliberate quoted artifact — per §6 of the
preregistration. **They are never pooled into any reported count or latency**,
and every artifact carries its population.

## 3b. The concrete 18 — FROZEN 2026-08-28, with one correction

The identities are frozen in
`SOLANA-SOCIAL-OBSERVER-QUALIFICATION-001-SOURCE-UNIVERSE.frozen.json` and
validated by `app/social/x_universe.py`. Quotas land exactly on §2: 4/3/3/4/2/2.

**Correction to §2.** Classes 5 and 6 were specified as *accounts* ("accounts
that post tickers without addresses", "known-impersonator accounts, if
identifiable"). Made concrete, they are **shape rules, not identities**, and
the loader now refuses a named handle in either class.

Two reasons. Ticker-only is a property of a *post*, not of an account — the
same account posts addresses on Monday and bare cashtags on Tuesday, so
freezing an identity into that class would mislabel it from the first post
that carried an address. And naming a specific real account as an impersonator
asserts a fact about a real entity that nothing here has verified; it is an
accusation, not a selection. The authority resolver already decides
`IMPERSONATOR` from mutual attestation at run time. The rule's only job is to
surface the candidate — which is what §2's "if identifiable" hedge was
already gesturing at.

**Handle resolution is a precondition, not an assumption.** All 14 named
accounts are frozen with `handle_resolved: false`. A `from:` rule against a
misspelled handle matches nothing and is indistinguishable from a quiet
source — the exact silent zero this qualification exists to prevent — so
`assert_activatable()` refuses until every handle resolves to a platform user
id. Resolution needs the network; `x_universe.py` imports only json,
dataclasses, enum, pathlib and typing, and a test asserts that. The smoke
performs the resolution and passes it in.

## 4. Cost envelope — hard, fail-closed

| field | value |
|---|---|
| provider | X API — Filtered Stream |
| sources | 18 (§2) |
| **max natural Post reads** | **3,000** |
| **approximate Post-read cost ceiling** | **~$15** |
| `max_artifacts_per_day` | 3,000 (the run is shorter than a day) |
| reconnect policy | resume the stream; reconnects do **not** reset the counter |
| backfill policy | **none fetched**; backfill would not count anyway (§5) |
| `on_exceeded` | **STOP** — the collector refuses to start and stops mid-run |

Enforced by `MonthlyReadCostGuard`, which is mandatory
(`CollectorNotStartableError` without one) and reserves per unit rather than
answering "how many are left?" — there is no path returning a boolean a caller
can ignore.

**CONTROL artifacts consume no Post-read budget.** They are injected, not read.

## 4b. The observer lifecycle — events in, states out

The transport **reports**; the driver **interprets**. `x_transport` emits
typed `TransportEvent`s and cannot reach `x_stream_state` at all — asserted
structurally, along with the absence of any `transition`, `note_frame` or
budget identifier in the transport.

Three things live in `observer_session.py` and deliberately not in the
transport: the event→state mapping, the Post-read budget, and the keepalive
stall deadline. A transport that knew its own budget could stop for a reason
the accounting never saw, and every future edit to reconnect handling would
silently be an edit to observation accounting. As events, the entire
lifecycle — including every failure path — replays from a list with no socket.

`tick()` exists because the two judgements the wire cannot report are both
about elapsed time: a wedged connection emits nothing, *including no event
saying so*. Its first version had the wall cap as an `elif` after the stall
check, so past the stall window a wedged stream masked the absolute 8-hour
bound. A hard stop that only applies when nothing else is wrong is not a cap;
the cap is now evaluated first and unconditionally, with a regression test
that sets up the masking condition on purpose.

## 5. LIVE only

Only `delivery_mode == LIVE` counts toward funnel and latency. Backfill is not
fetched at all for this qualification; were it present, its receipt timestamp
would measure when we *fetched*, not when it *arrived*.

## 6. Stopping rule — frozen before the run

Stop at the **first** of:

* **≥ 4 hours elapsed AND ≥ 250 natural artifacts observed** — the intended
  terminal condition;
* **8 hours elapsed** — absolute cap, whatever the count;
* **3,000 natural Post reads** — cost ceiling;
* a preregistered safety/system stop.

Both the time *and* count conditions must hold for the normal stop, so a quiet
stream cannot end the run early and a busy one cannot end it before four hours
of clock coverage.

## 7. System failure is not funnel loss

**These are different facts and are reported separately.**

| | |
|---|---|
| funnel loss | an artifact legitimately refused — no mint candidate, unattested source, not canonical, not live, clock not computable |
| system failure | a disconnect, a rate-limit, an RPC outage, a crash, a rule-sync error |

A stream that dropped for an hour must not appear as "the market was quiet",
which is the shape S04 taught. The run reports connected time, disconnects,
reconnects, keepalives seen, rate-limit events, and RPC `UNAVAILABLE` counts
**alongside** the funnel, and any artifact lost to a system failure is recorded
as such rather than as a refusal.

**Artifact conservation:** every artifact received is accounted for — reached a
stage, refused with a type, or lost to a named system failure. Received must
equal the sum.

## 8. Reported outputs

The frozen funnel counts, per-stage losses, largest loss, typed refusal counts,
`L_delivery` and `L_pipeline` **reported separately and never summed**, plus the
system-health counters of §7.

**Not computed, and not computable by this code:** returns, markouts, token
performance, source rankings, source scores, or any trading decision.

---

## 9. BLOCKERS — why this cannot run today

Recorded plainly rather than worked around.

1. **There is no live X transport.** `app/social/transport.py` ships
   `FixtureTransport` and `NullTransport` only; the latter is the default and
   raises `LiveTransportUnavailableError: "SOCIAL-TAPE-001 ships no live
   transport and activates nothing"`. A real streaming client does not exist.

2. **There are no X credentials.** No bearer token is configured on either host
   and no credential store contains one. Filtered Stream also requires a paid
   API tier, which is a purchasing decision, not an engineering one.

Neither is a defect — SOCIAL-TAPE-001 deliberately shipped the boundary without
the connection. But it means the correct next milestone is
`SOCIAL-X-LIVE-TRANSPORT-001`: a streaming client behind the existing
`SocialStreamTransport` Protocol, credential handling, rule sync against the
frozen universe, and reconnect/keepalive semantics — qualified against
`FixtureTransport` first.

**The universe and envelope above are frozen now so that they precede the
connection rather than being chosen once counts are visible.** That was the
point of freezing them today.
