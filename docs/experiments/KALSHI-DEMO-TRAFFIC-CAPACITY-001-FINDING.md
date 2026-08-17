# KALSHI-DEMO-TRAFFIC-CAPACITY-001 — findings

**Companion to the machine-generated artifacts** in
`KALSHI-DEMO-TRAFFIC-CAPACITY-001-RUNS/`, which are pure tool output and are
reproducible from the tools in `scripts/`. This file is the human analysis, the
controls, and the list of things that could **not** be determined.

**Read-only throughout.** Market-data channels only. No orders, no portfolio
channels, no venue writes, no capital, no archive qualification run, and
**nothing was archived** — every session ran with `archive_root=None`, so
`_Session._archive` stayed `None` and `EventArchive.append` was never called.
**This is not CP6.** It borrows CP6's `--dry-run` mechanism and claims no
checkpoint.

---

## 0. THE ANSWER

> **Is `100,000 frames in 4 hours` realistically attainable on DEMO?**

## **No. VERDICT: UNREACHABLE.**

The frozen twelve-market pool produced **679 frames in 900 seconds**.

| quantity | value |
|---|---:|
| Σλ̂ (point) | **0.7533 frames/s** |
| Σλ̂ (95% one-sided lower bound) | 0.5367 frames/s |
| **N_4h point estimate** | **10,848** |
| N_4h lower bound | 7,728 |
| preregistered floor | 100,000 |
| **shortfall** | **9.2×** |

The rule is `UNREACHABLE` when the **point estimate itself** is below 100,000.
It is below by an order of magnitude, so the verdict does not turn on the
interval, on the interval's method, or on any judgement about how conservative
to be. Nothing in the frozen rule was adjusted.

**This is a finding about DEMO, not a probe failure**, and it is not a finding
about production (§7.5). It reshapes the CP6–CP9 qualification design; §10 sets
out the options it leaves open, without picking one.

---

## 1. Field semantics FIRST — what a "frame" is, verified before it was counted

Doctrine 8, and the reason this milestone exists: the tape manifest gated on
`updated_time` and produced a precise, dramatic, reproducible, completely wrong
finding because a field name was trusted. So the counter was characterised
before its number entered a statistic.

### 1.1 Four independent counters, and they agree

`events_received` is maintained by the collector, `frames_received` and
`frames_yielded` by the transport, and `frames_tapped` by this milestone's
tap — four counters in three modules, incremented at three different points on
the path.

| run | tapped | transport received | transport yielded | session received |
|---|---:|---:|---:|---:|
| main-pool-12-900s | 679 | 679 | 679 | 679 |
| control-test-instruments-200 | 68,370 | 68,370 | 68,370 | 68,370 |
| control-wide-eligible-200 | 768 | 768 | 768 | 768 |
| control-wide-eligible-all-388 | 1,546 | 1,546 | 1,546 | 1,546 |

`frames_malformed = 0` everywhere, so no frame was dropped between the socket
and the count.

### 1.2 What actually moves the counter — observed, not assumed

**One venue WebSocket text message = one counted frame = one `append()` the
collector would have made.** The tap sits on the transport's frame iterator,
which is the same iterator `_one_connection` consumes, and `_handle_frame`
increments `events_received` once per message and calls `append()` at most once
per message. There is no batching, fan-out or coalescing anywhere on that path.

The frame types actually observed, and what each one is:

| `type` | carries `msg.market_ticker` | when it arrives | counted? |
|---|---|---|---|
| `subscribed` | **no** | 3 per subscription generation — **one per channel**, not one per market | yes |
| `orderbook_snapshot` | yes | **one per market per subscription generation** | yes |
| `orderbook_delta` | yes | continuously | yes |
| `ticker` | yes | continuously | yes |
| `trade` | yes | continuously (never in the pool window) | yes |
| `error` | no | once, in each ≥200-market run | yes |

Two of these are **one-off**, not rates: a 12-market subscription costs 3
`subscribed` + 12 `orderbook_snapshot` = 15 frames once, and multiplying a
handshake by 14,400 would be a fabricated rate. Every number below is therefore
reported in two arms — **all archived frames** (what the 100,000 floor counts)
and **continuous frames only** (one-off types removed). For the twelve-market
pool the two arms differ by 2%; for a 388-market universe they differ by 34%,
which is exactly why both are carried.

### 1.3 The instrument artefact that was found and removed

The first pilot ran at the transport's shipped `read_timeout_s = 60`. On a
quiet pool that trips `TransportReadTimeout` after 60 s of silence, the
collector reconnects, and **the resubscribe replays one `orderbook_snapshot`
per market**. Left in, a silent venue would have measured as a mildly active
one, with periodic frame bursts that were entirely our own reconnect ladder.
The measurement runs therefore set `read_timeout_s` above the session length so
that silence stays silent. Every measurement run below has **`reconnects = 0`
and exactly one connection**, so no snapshot was ever replayed.

### 1.4 What this probe measures is an UPPER bound on archived frames

In dry-run the archive is never constructed, so `events_rejected` is
structurally 0 and a frame the writer would have refused is indistinguishable
from one it would have accepted. `events_received ≥ events_archived` always.
The reported `N_4h` is therefore an **upper** bound on archived frames, which
only strengthens an UNREACHABLE verdict.

---

## 2. The pool — frozen and committed before the socket was opened

`KALSHI-DEMO-TRAFFIC-CAPACITY-001-POOL.json`, produced by
`scripts/kalshi_demo_capacity_freeze_pool.py` from the committed manifest
artifact and **committed in `ee5ee75`, before any run**. Twelve markets, twelve
distinct events, matching the qualification session's `universe_size` so `N_4h`
is directly comparable to it. `KXMAXSHARDINGTEST` and `KXTESTMATCH` are
excluded per the approved amendment.

- **High (all four, ranks 1–4, no discretion):** `KXMLB-26-TEX` (16,890
  c/min), `KXNBA-27-TOR` (13,176), `KXLIGAMXGAME-26AUG16SLACDG-SLA` (5,419),
  `KXECONSTATCPIYOY-26AUG-T3.6` (2,632).
- **Plateau (15–17 c/min band, manifest rank order, manifest's own
  `within_stratum_pick`):** `KXPGATOUR-FESJC26-ARAI`, `KXHEISMAN-27-CCARR`,
  `KXSB-27-KC`, `KXPRESNOMR-28-ITRU`, `KXNASCARRACE-COOO81526-ROCH`,
  `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-14`, `KXPGATOP20-FESJC26-NHOJ`,
  `KXPGATOP10-FESJC26-CCON`.

Eleven of the twelve turned out to be inert. **They stayed in**, and their
rates are reported in §3.

---

## 3. The measurement

`main-pool-12-900s`, DEMO, from EVO, `2026-08-17T05:51:15.983071Z` →
`06:06:26.679151Z`. One connection, 0 reconnects, 0 malformed frames, 0
sequence faults, 900.35 s observed, 5-second observation bins (the
preregistered 2–5 s cadence), channels `orderbook_delta` + `ticker` + `trade`.

**679 frames total: 641 `orderbook_delta`, 23 `ticker`, 12
`orderbook_snapshot`, 3 `subscribed`, 0 `trade`.**

### 3.1 Per-market rates — the whole pool

| market | stratum | manifest c/min | frames in 900 s | λ̂ (frames/s) | frames per 4 h at this rate |
|---|---|---:|---:|---:|---:|
| `KXECONSTATCPIYOY-26AUG-T3.6` | high | 2,632.11 | **664** | 0.7378 | 10,624 |
| `KXMLB-26-TEX` | high | 16,890.34 | 1 | 0.0011 | 16 |
| `KXNBA-27-TOR` | high | 13,175.99 | 1 | 0.0011 | 16 |
| `KXLIGAMXGAME-26AUG16SLACDG-SLA` | high | 5,419.04 | 1 | 0.0011 | 16 |
| `KXPGATOUR-FESJC26-ARAI` | plateau | 17.00 | 1 | 0.0011 | 16 |
| `KXHEISMAN-27-CCARR` | plateau | 16.97 | 1 | 0.0011 | 16 |
| `KXSB-27-KC` | plateau | 16.97 | 1 | 0.0011 | 16 |
| `KXPRESNOMR-28-ITRU` | plateau | 16.96 | 1 | 0.0011 | 16 |
| `KXNASCARRACE-COOO81526-ROCH` | plateau | 16.94 | 1 | 0.0011 | 16 |
| `KXVOTEPRIMARY-GOVFLNOMR26JFISJFIS-14` | plateau | 16.94 | 1 | 0.0011 | 16 |
| `KXPGATOP20-FESJC26-NHOJ` | plateau | 16.92 | 1 | 0.0011 | 16 |
| `KXPGATOP10-FESJC26-CCON` | plateau | 16.90 | 1 | 0.0011 | 16 |

**The single frame each of the other eleven markets produced is its
`orderbook_snapshot`.** Eleven of twelve markets emitted **zero** continuous
frames in fifteen minutes — including the three markets the manifest ranked
1st, 2nd and 3rd by traded contracts per minute.

`frames_attributed_to_non_pool_tickers` is **empty**: every frame carrying a
ticker carried a ticker that was subscribed. The counter attributes; it does
not invent.

### 3.2 N_4h, its interval, and the method

`N_4h = 14,400 × Σλ̂`, exactly as frozen.

| arm | frames | Σλ̂ | **N_4h point** | N_4h 95% lower | N_4h 95% upper |
|---|---:|---:|---:|---:|---:|
| all archived frames | 678 | 0.7533 | **10,848** | 7,728 | 14,016 |
| continuous frames only | 663 | 0.7367 | **10,608** | 7,520 | 13,744 |

(180 complete 5-second bins; the truncated final bin is dropped.)

**Method: circular moving-block bootstrap on the per-bin pool total**, 60-second
blocks, 20,000 draws, one-sided 5th percentile, seed 20260817.

Two dependencies made the obvious alternative wrong, and the preregistration
names both. **Per-market rates are not independent within an event** — the pool
holds three markets from the same PGA tournament (`FESJC26`, across the
`KXPGATOUR`, `KXPGATOP20` and `KXPGATOP10` series), and a single simulated
market maker would move the whole plateau together — so summing independent
per-market Poisson intervals would understate the variance of the sum by
whatever the cross-market correlation is. And **frames are serially
dependent**: the bins are visibly not i.i.d., alternating between long runs of
zero and bursts that land on exactly 20. So nothing is ever
combined across markets: the resampling unit is the bin total, already summed
across the pool, which carries the cross-market dependence without modelling
it; and contiguous blocks of bins are resampled rather than single bins, which
carries the serial dependence. No Poisson assumption is made anywhere.

**What the interval does not cover, stated plainly.** It bounds sampling
variability *within* the observed window. It cannot bound variation *between* a
short window and a four-hour session. See §7 for what that leaves open.

To clear the floor, this pool would need **6.94 frames/s sustained for four
hours**. It produced 0.75, and **97.9% of that came from one market**
(λ̂ = 0.7378 of Σλ̂ = 0.7533).

---

## 4. Positive control — the counter is not broken (doctrine 7)

A zero is the one measurement that cannot interpret itself: a quiet venue and a
broken subscription produce byte-identical zeroes, and doctrine 7 says absence
is not health. So the underlying condition was forced.

**Arm: `KXMAXSHARDINGTEST` / `KXTESTMATCH`, 194 markets, 300 s.** These are the
venue's own load-test instruments, already excluded from the qualification
universe by the approved amendment, so whatever they do cannot contaminate the
statistic.

| | |
|---|---:|
| frames received in 299.72 s | **68,370** |
| frames in the 295 s of complete bins (continuous arm) | 66,380 |
| Σλ̂ | **225.0 frames/s** |
| **N_4h** | **3,240,244** (lower bound 3,123,792) |
| verdict the *same* estimator returns | **REACHABLE** |

The same socket, the same signer, the same subscription code, the same tap, the
same collector dry-run path and the same decision rule that report 10,848 for
the pool report **3.24 million** here, and the verdict flips to REACHABLE. The
metric becomes non-benign when the condition holds. 22 MB in five minutes moved
through the identical path with 0 malformed frames and 0 reconnects.

The instrument is sound. **The pool's silence is the venue's, not ours.**

---

## 5. Independent corroboration — the REST cross-check

The same twelve markets, read twice nine minutes apart over a completely
different route (public `GET /markets`, no credential, no socket), spanning the
same window as the primary run. **Every field the venue returns was diffed**,
not an allowlist — picking fields by name before knowing what drives them is
the exact error that produced the `updated_time` incident.

| field | markets that moved (of 12) |
|---|---:|
| `volume_fp` (lifetime, monotone) | **0** |
| `yes_bid_dollars` / `yes_ask_dollars` (top of book) | 0 |
| `updated_time` | 0 |
| `status` | 0 |
| `open_interest_fp` | 0 |
| `yes_bid_size_fp` / `yes_ask_size_fp` | **1** |
| `volume_24h_fp` | **10** |

Three things follow, and they agree exactly with the socket.

1. **No trades occurred.** The lifetime counter — the manifest's own chosen
   statistic, monotone by construction — did not move on a single market in
   nine minutes. That is why there were zero `trade` frames.
2. **The one market whose book moved is the one market that sent frames.**
   `yes_bid_size_fp` and `yes_ask_size_fp` moved on exactly one market:
   `KXECONSTATCPIYOY-26AUG-T3.6`, the market that produced 665 of the 679
   frames. Two entirely independent routes, one market, same answer.
3. **All ten `volume_24h_fp` moves are DECREASES.** Roll-off out of the trailing
   window, with nothing arriving to replace it — an independent replication of
   the manifest's finding that a 24-hour field's difference is not an arrival
   rate and can go negative.

---

## 6. THE STRUCTURAL FINDING — DEMO's message volume is a load test

The manifest reported that 194 of 582 eligible markets (33%) are venue test
instruments and flagged it without acting on it. Measured on the wire, that
understates it severely.

| subscription | markets | continuous frames/s | **N_4h (continuous)** |
|---|---:|---:|---:|
| **`KXMAXSHARDINGTEST` + `KXTESTMATCH`** | 194 | **225.0** | **3,240,244** |
| all 388 eligible non-test markets | 388 | 3.84 | 55,344 |
| top 200 eligible non-test markets | 200 | 1.88 | 27,024 |
| the frozen 12-market pool | 12 | 0.74 | 10,608 |

The two disjoint arms cover the whole eligible population the manifest found.
Put side by side, **98.3% of the frames the eligible population emits come from
194 venue test instruments** and 1.7% from the 388 real markets.

Within the test arm, the 188 `KXMAXSHARDINGTEST` markets emit a metronomic ~363
frames each per five minutes (median 363; the top five, 380–385, are barely
above it); the six `KXTESTMATCH` markets emit only their snapshot. **DEMO's
WebSocket traffic is, to a first approximation, one sharding load-test rig**,
and the manifest's "four real markets plus a synthetic plateau" reading of the
REST distribution is confirmed from a second, independent direction.

### 6.1 The consequence that matters for the qualification design

**Subscribing to the ENTIRE real-market eligible universe of DEMO — all 388
non-test markets, 32× the preregistered universe size — still does not reach
the floor.** 55,344 frames in four hours, point estimate, 55% of 100,000. The
`all archived frames` arm reaches 74,160 only because it multiplies 388 one-time
snapshots by 14,400, which is not a rate.

So `100,000 archived live frames in ≤ 4 hours` is not a pool-selection problem
on DEMO. There is no pool of real DEMO markets that reaches it. The floor and
the venue are incompatible as currently written.

---

## 7. What could NOT be determined

1. **Whether a four-hour session could catch a burst the short windows missed.**
   This is the real limitation and it is not small. The manifest measured
   `KXMLB-26-TEX` at **16,890 contracts/min** on 2026-08-16 04:01–04:09Z; over
   nine minutes on 2026-08-17 05:55–06:04Z its lifetime volume moved **zero**,
   and over fifteen minutes it sent **zero** frames. DEMO's flow is episodic on
   a scale longer than the observation window, so the bootstrap interval —
   which is a within-window interval — cannot bound a four-hour total. What can
   be said is that the shortfall is **9.2× for the pool and 1.8× for the entire
   non-test universe**, and that eleven of twelve pool markets contributed
   literally nothing, so the whole four-hour budget would have to arrive as
   bursts. §8 records the longer replication run against this exact concern.
2. **Whether the pool was quiet because 26 hours had passed.** The pool was
   frozen from a snapshot taken 2026-08-16T04:01Z and probed 2026-08-17T05:51Z.
   The manifest itself flagged (§6.5) that three of the four high-activity
   markets are event-driven and might not stay active;
   `KXLIGAMXGAME-26AUG16SLACDG` is a 16 August fixture and had almost certainly
   concluded (its `status` was not re-read for a value, only for a change, and
   it did not change). That confound is real and cannot be separated from
   episodic flow by this design. It does not change the verdict, because the
   388-market universe — probed nineteen minutes after the primary run, from a
   list that includes every market the manifest found eligible — also falls
   short.
3. **The rejection rate.** Dry-run cannot observe `events_rejected` (§1.4), so
   the archived count is bounded above but not below.
4. **Whether DEMO's flow is simulated.** Still inference from shape, though the
   shape is now much sharper: 188 load-test markets each emitting a metronomic
   ~1.21 frames/s, and bursts of exactly 20 frames in the pool window.
5. **Anything at all about production.** A DEMO rate is not a production rate.
   Nothing here licenses a claim about production capacity, and CP10's separate
   Tier-2 approval is unaffected by this document.
6. **The `error` frame's body.** Its *cause* was determined (§9.1) — it is the
   venue rejecting the collector's own recovery command — but the message body
   was not captured, so the venue's error code is unknown.

---

## 8. Replication

*(see §8.1)*

---

## 9. Three observations for CP6 — flagged, not acted on

All three were visible in these runs and none is in this milestone's scope.
They are recorded because a venue-model or bound surprise found during
qualification contaminates everything downstream, and the CP6–CP9
preregistration says so.

0. **`max_seconds` is not enforced during silence.** `CollectorConfig`
   documents `max_seconds`, `max_events` and `max_reconnects` as "three
   independent hard caps" and says "there is no unbounded session". But
   `_cap_check()` is reached **only from `_handle_frame`**, so all three caps
   are evaluated on frame arrival and never on a timer. While the venue is
   quiet the session is blocked in `recv()` and no cap can fire; the only thing
   that ends it is the transport's `read_timeout_s`, and then only to
   reconnect. Found the hard way: a session launched at `06:34:43Z` with
   `max_seconds=3600` was still connected, still frameless and had written no
   result at `07:57Z` — **86 minutes into a 60-minute bound.** The session was
   killed rather than left running. Two consequences: a 4-hour-capped
   qualification session can overrun its bound on a quiet venue, and — since
   the overrun was caused by nothing arriving — this is also **direct evidence
   that the frozen pool stayed silent for well over half an hour**, from a run
   that was not designed to measure it.

1. **Every `trade` frame produces a `sequence_fault`, and the recovery it
   triggers is REFUSED by the venue.** The chain is exact in all three runs
   that saw trades, and absent in the one that did not:

   | run | trades | sequence faults | recoveries requested | `error` frames |
   |---|---:|---:|---:|---:|
   | control-test-instruments-200 | 1,776 | **1,777** | 1 | 1 |
   | control-wide-eligible-all-388 | 230 | **231** | 1 | 1 |
   | control-wide-eligible-200 | 56 | **57** | 1 | 1 |
   | main-pool-12-900s | 0 | **0** | 0 | 0 |

   Faults = trades + 1 every time; the +1 and the `error` are the same event.
   The first `trade` frame is treated as a sequence fault, the collector sends
   one `get_snapshot` recovery, the venue answers `error`, and that error is
   itself counted as a fault. Every subsequent trade faults too and is
   correctly *not* re-recovered — `_recovery_pending` does its job and there is
   no command storm — but the subscription never regains health, so **on a
   venue with trade flow the collector spends the whole session in a permanent
   fault state.** Whether `trade` frames carry no `seq` or a `seq` on a
   different `sid` was not determined, and the error body was not captured.
   **CP7 should not be attempted before this is understood**: CP7's proof is
   that a generation boundary drops and re-acquires publishability, and it
   would be reading a counter that is already pinned high for an unrelated
   reason.
2. **`orderbook_snapshot` on DEMO carries no ladder.** The observed message
   shape is `{market_id, market_ticker, no_dollars_fp, yes_dollars_fp}` — no
   `yes`/`no` level arrays. If the fixtures assume a laddered snapshot, that is
   a semantic mismatch with the venue, and CP6 §2 says stop and report on
   exactly this.

---

## 10. What this leaves open for Eric

Stated as options, not a recommendation with one pre-picked. The finding
constrains the qualification design; it does not choose for it.

1. **Lower the frame floor to something DEMO can produce.** ~50,000 is
   reachable from the full non-test universe; ~10,000 from a twelve-market
   pool. This is a preregistration amendment and must be made explicitly,
   before any session, with its effect on CP9's power stated — CP9 is
   underpowered for p99 latency at 10k frames and the preregistration already
   anticipates that outcome in writing.
2. **Keep the floor and change the universe size.** A twelve-market session
   cannot reach even a reduced floor; the universe would have to grow by more
   than an order of magnitude, which changes what "stratified by activity"
   means.
3. **Keep the floor and accept the 4-hour cap being hit short.** The
   preregistered stop rule already says reaching the cap short of 100,000 is a
   finding, not a reason to extend. This probe says that outcome is not a risk
   but a certainty, which makes running the session to discover it a choice
   about whether the other CP6–CP9 proofs are worth four hours on their own.
4. **Use the test instruments deliberately for the throughput proofs.** They
   deliver 225 frames/s and would satisfy any frame-count floor in minutes.
   They are worthless for microstructure and are excluded from the qualification
   universe by an approved amendment — but a rotation-and-backpressure proof is
   not a microstructure question, and running the two on separate instruments
   would be a design change, stated as one.
5. **Go to production for the rate distribution.** Changes the credential and
   risk profile; separate Tier-2 approval (milestone §11 Q1, CP10).
6. **Do nothing.** The pool is frozen, the answer is recorded, and no capture
   has occurred.

---

## 11. Safety

Read-only throughout. Market-data channels only (`orderbook_delta`, `ticker`,
`trade`), all inside `kalshi.ALLOWED_CHANNELS`, checked by
`assert_channels_allowed` at `CollectorConfig` construction before any object
existed that could open a socket. `commands_refused = 0` and the only commands
sent were `subscribe` and `get_snapshot`. No order, position, wallet or
key-management surface was reached; the observer signer holds only
`WEBSOCKET_HANDSHAKE`.

**The credential was proven read-only against the venue before the WebSocket
signer was built** — one signed `GET /trade-api/v2/api_keys`, scopes `["read"]`,
`proven_read_only = true`. The bootstrap signer used for that audit holds only
`API_KEY_METADATA` and cannot reach the handshake. **No key material, no key
id, no signed URL was copied, printed or logged**; the run artifacts record a
key-id fingerprint and nothing else.

**Nothing was archived.** Every session ran `dry_run=True` with
`archive_root=None`; `events_archived = 0` and `segments_committed = 0` in every
run.

`app/` was not modified. The measurement is a delegating transport wrapper on
the existing `transport_factory` seam, so the collector ran exactly as shipped.

EVO's production checkout was never modified: the scripts ran from
`/tmp/kalshi-capacity-001` with `PYTHONPATH` pointing at the checkout, and
`~/projects/probability-arena` stayed on `main` at `d7fd8d1`, working tree
clean, before and after.
