# KALSHI-COLLECTOR-P0-FIXES — the wire evidence, and what it changed

**Status: fixed, proven offline and live. Branch `KALSHI-COLLECTOR-P0-FIXES`,
not merged.**

Three defects were flagged in `KALSHI-DEMO-TRAFFIC-CAPACITY-001-FINDING.md` §9
and deliberately not acted on there. All three were in our collector; none was a
DEMO artefact. This document records what the venue actually said — captured
**before** anything was patched, per doctrine 8 — and what each answer changed.

Read-only throughout. Market-data channels only, `dry_run=True`,
`archive_root=None`; the only commands that reached a socket were `subscribe`
and the `get_snapshot` whose refusal was the thing being measured. EVO's
checkout was never modified and was verified clean before and after every run.

---

## 0. THE ANSWERS, in one table

| question | the venue's answer | consequence |
|---|---|---|
| does a `trade` frame carry `seq`? | **yes** | it participates in ordering |
| on which `sid`? | **its own** — sid 3, not the orderbook's sid 1 | it is a separate sequence space with no snapshot |
| what is in the `error` body? | `{"code":13,"msg":"Unsupported action"}` | `get_snapshot` is invalid on a trade subscription |
| is `get_snapshot` valid anywhere? | **yes, on the orderbook sid** — answered with three snapshots, no error, no rewind | the recovery path is sound; it was misaimed |
| does `orderbook_snapshot` carry a ladder? | **57 of 60 did** | §9.2's "carries no ladder" is FALSE as stated |
| does `ticker` carry `seq`? | **no — 2,071 of 2,071 without one** | that channel has no drop detector at all |

Artifacts: `KALSHI-COLLECTOR-P0-FIXES-RUNS/`. Probes:
`scripts/kalshi_collector_p0_wire_probe.py`,
`scripts/kalshi_collector_p0_recovery_probe.py`.

---

## 1. Method — why the previous evidence could not settle any of this

The capacity probe recorded, per frame, the top-level **key names** and the
`msg` **key names**. That is enough to count frames and it is not enough to
answer a single question above, because a key list is not a value and an absent
key sampled 30 times is not a statement about the other 8,149 frames.

So this probe keeps whole frame **bodies** for a bounded sample of each type,
every `error` frame and every `subscribed` ack verbatim, the outbound commands
in order with the frame ordinal at which each was sent — so a reply can be
paired with the command that caused it — and a per-`sid` ordering census that
counts contiguity, gaps, duplicates and regressions separately. Ladder-bearing
and ladder-less snapshots are sampled into **separate** buckets, so a run cannot
answer "does a snapshot carry a ladder" with whichever kind happened to arrive
first, and "key absent" is never merged with "key present holding an empty list".

Decimals are rendered with `str()`, never `float()`. The transport parses venue
numbers exactly on purpose; a probe that undid that would be recording a
different number from the one the venue sent.

The collector ran **exactly as shipped** underneath a delegating transport tap,
so nothing measured here is an artefact of an instrumented build.

**Run:** `p0-wire-test-instruments-60`, 2026-08-17T08:49Z, 60 venue test
instruments, channels `orderbook_delta,ticker,trade`, 120 s, 8,179 frames.

---

## 2. Defect 1 — the trade stream was never a book

### 2.1 What the venue sends

The three `subscribed` acks, verbatim, from one `subscribe` naming three
channels:

```json
{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":1}}
{"id":1,"type":"subscribed","msg":{"channel":"ticker","sid":2}}
{"id":1,"type":"subscribed","msg":{"channel":"trade","sid":3}}
```

**One sid is one channel, and the venue says so itself.** Note also that the ack
carries no *top-level* `sid` — the sid is inside `msg` — so an ack is not
routable to a subscription and cannot be the source of this mapping in code.

A `trade` frame, verbatim:

```json
{"type":"trade","sid":3,"seq":1,
 "msg":{"count_fp":"2.00","market_ticker":"KXMAXSHARDINGTEST-26AUG2818-T57399.99",
        "no_price_dollars":"0.2000","taker_book_side":"ask",
        "taker_outcome_side":"no","taker_side":"no",
        "trade_id":"879217f6-553e-7222-2304-fb05558d63d0",
        "ts":1786956599,"ts_ms":1786956599391,"yes_price_dollars":"0.8000"}}
```

It carries `seq`. It carries it on **its own** sid.

### 2.2 The census that makes this damning

| sid | channel | frames | `seq` present | contiguous steps | gaps | duplicates | regressions |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | orderbook_delta | 5,886 | 5,886 | 5,885 | **0** | 0 | 0 |
| 2 | ticker | 2,071 | **0** | — | — | — | — |
| 3 | trade | 219 | 219 | 218 | **0** | 0 | 0 |

The trade subscription ran `seq` **1 … 219 with no hole of any kind** — and the
collector reported **219 sequence faults on it**. The stream was clean the whole
time; the detector was wrong the whole time.

### 2.3 Why

`SubscriptionState.healthy` means *a snapshot has based this book*. It exists so
an `orderbook_delta` is never applied to a book with no base — a delta on an
unbased book fabricates a book. It was applied to **every** sid. The trade sid
never receives an `orderbook_snapshot`, so it could never become healthy, so
every frame on it was refused with `SUB_AWAITING_SNAPSHOT`.

Then the recovery made it worse. The collector answered the first fault with
`get_snapshot` aimed at sid 3, and the venue replied:

```json
{"type":"error","sid":3,"seq":2,"msg":{"code":13,"msg":"Unsupported action"}}
```

**That error consumed seq 2 on the trade subscription** — visible in the census
as the 219th frame on a stream of 218 trades. So the invalid recovery did not
merely fail; it inserted a frame into the very sequence space it had just
misjudged. `faults = trades + 1` in every run is that `+1`.

### 2.4 The control that turns this from a symptom into a diagnosis

The obvious reading of code 13 is "`get_snapshot` is not supported" — which
would mean the collector has **no** recovery path at all, a far larger finding.
`scripts/kalshi_collector_p0_recovery_probe.py` settles it: subscribe to
`orderbook_delta` alone, wait for a real snapshot, send exactly one
`get_snapshot` on the **orderbook** sid, and record the reply.

The venue **answered it** — three `orderbook_snapshot` frames at seq 31, 32, 33,
one per subscribed market, no error, and continuing the same sequence stream so
`apply_snapshot`'s anti-rewind guard is satisfied. Deltas resumed at seq 34.

**The recovery works. It was being aimed at a subscription that has no snapshot
to give.**

### 2.5 The fix

* `SubscriptionState.accept(..., needs_base=False)` separates **ordering** from
  **basing**. On a stream with no snapshot the first `seq` seen *anchors* it and
  continuity is checked from there. A gap still faults — the drop detector is
  armed on the first frame instead of never.
* After a discontinuity on an unbased stream, the position **re-anchors**
  (`_reanchor_if_unbased`). Without this, one gap would fault every later frame
  forever and the counter would measure *time since the gap* rather than the
  *number of gaps* — the same pinned-counter defect one level down.
* `get_snapshot` is sent only to a subscription **observed** to carry orderbook
  frames (`SubscriptionState.carries_orderbook`, set by the router on the first
  orderbook frame — derived from frames, not from the subscribe command, so the
  live lane and the replay lane reach the same verdict). There is no repair for
  a lost trade print and the fix does not pretend otherwise: the fault is
  counted, the stream re-anchors, nothing is sent.

### 2.6 Why 368 green tests never saw it

The pre-existing CP3 fixtures put **every channel on one shared sid**. On that
model the orderbook snapshot bases the subscription and the trade frames order
against it perfectly. The venue does not do that. A fixture that models the
venue wrongly is a test that passes for the wrong reason, and this one passed
for the wrong reason 368 times.

---

## 3. Defect 2 — the reported defect is false; the real one is ours

§9.2 said DEMO's `orderbook_snapshot` "carries no ladder", from an observed
shape of `{market_id, market_ticker, no_dollars_fp, yes_dollars_fp}`. Two things
are wrong with that. The shape quoted **does** contain both ladder keys, and the
sample it was drawn from recorded key names only.

Measured with values, 60 snapshots in one run:

| ladder shape | count |
|---|---:|
| both sides present, 1–8 levels each | **57** |
| `yes` absent **and** `no` absent | **3** |

The census **replicates exactly** across the before and after runs — 57/3 both
times, on separate sockets. So the venue does send ladders, and it omits a
side's key when that side holds nothing, which `book.py` had already recorded as
legitimate from a 2026-08-08 capture.

**The real defect is the opposite one, and it is in our normalizer.**
`msg.get("yes_dollars_fp") or []` collapsed *absent* into *empty*, so a snapshot
that transmitted no ladder produced `coverage[bid_levels] = "present"` with zero
levels, and `depth` was hardcoded to `"full_ladder"` whatever arrived. A typed
absence wearing the word "present" is exactly the plausible-benign-value class
doctrine 7 names — and downstream it means a market with **no observation** on a
side is indistinguishable from a market **observed to have no liquidity** there.
3 of 60 snapshots hit it on an ordinary run.

Fixed:

* an omitted ladder key is `ABSENT_NOT_SUPPLIED`; an explicitly empty list stays
  `PRESENT`, because "the venue said there is nothing here" is an observation;
* `depth` is one of `full_ladder` / `one_side_ladder_only` / `no_ladder_supplied`;
* `OrderBook.ladder_presence` is typed (`supplied` / `omitted_by_venue` /
  `no_snapshot_applied`) and is carried beside every level count in
  `top_of_book()`, `yes_scale_ladder()` and the snapshot result, so a zero-level
  side always says **why** it is zero.

No fabricated depth is introduced anywhere: a ladder that was not transmitted
produces no levels, and now also produces a reason.

---

## 4. Defect 3 — `max_seconds` was a comment

`CollectorConfig` documents three "independent hard caps" and states "there is
no unbounded session". `_cap_check()` was reached only from `_handle_frame`, so
all three were evaluated **on frame arrival**. On a quiet venue the session sits
in `recv()` and nothing can end it: a session launched with `max_seconds=3600`
was found still connected, still frameless, 86 minutes in, and had to be killed.

Fixed with one `asyncio.wait_for` around the read loop, sized to what is left of
the budget. Not a polling loop and not a second task racing the reader: the
timer is the timeout argument of the very await the reader is already suspended
in, so between frames it costs one timer handle and zero wakeups.

It cannot interfere with a frame in flight. The only await points inside the
loop are the transport's `recv` and the recovery `send`; `archive.append()` is
synchronous and caller-threaded, so no suspension point exists between a frame
being read and its record being durable, and a cancellation cannot land between
them. What a cancellation can discard is a `recv` that had not yet produced a
frame — which is the session ending, not a frame being lost.

The reconnect ladder is bounded by the same budget: an exhausted budget buys no
further handshake.

`max_events` remains frame-driven because it cannot be approached any other way.
`stop_requested` is deliberately given no timer — it is an operator's intent, not
a bound, and waking the reader forever on a fixed cadence to ask a question that
is almost always "no" is the busy-wait this fix was told to avoid. It is no
longer ignorable indefinitely, because the session is now hard-bounded in time.

---

## 5. Proofs

`tests/test_kalshi_collector_p0_fixes_001.py` — 23 tests, fixtures copied
verbatim from the wire capture, on the sids the venue actually uses. **13 of the
23 fail against the unfixed collector**, verified by stashing the two changed
files and re-running; the 10 that pass in both states are the anti-vacuity
guards, which is precisely what they are for.

| forced condition | must become | must NOT become |
|---|---|---|
| real trade frames on their own sid | `sequence_faults == 0`, no command sent | a real gap on that same stream still faults |
| a gap on the trade stream | exactly **one** fault per gap | the counter is not pinned by the first |
| a genuine orderbook gap | faults, and **one** recovery aimed at sid 1 | — |
| a delta before any snapshot | still faults | — |
| a ladderless snapshot | `no_ladder_supplied`, `absent:not_supplied_by_venue` | a laddered snapshot still `present` and still reconstructs |
| an explicitly empty ladder | `present` | not conflated with absent |
| a silent session | ends at `max_seconds` | the test is wrapped in an outer `asyncio` timeout, so a regression **fails** instead of hanging the suite |

### 5.1 Suite state, and the attribution of every failure

`pytest -q -p no:randomly`, this host: **5,132 passed, 7 failed, 6 skipped,
4 xfailed** in 12 m 49 s. The kalshi subset alone (`-k kalshi`) is **1,654
passed, 0 failed**.

The seven are the known wall-clock/staleness flake class and none of them is
attributable to this change. Three independent checks:

1. **All seven pass in isolation** — 11 tests, 5.5 s, green.
2. **The visible assertion is a duration bound blown by suite length**:
   `assert 120 <= 943.1 <= 900` on `market_quote_age_s`, from a fixture seeded
   three minutes before module import in a run that took 12 m 49 s.
3. **None of the four files imports either changed module.** The only
   `realtime` occurrence across them is the `enable_realtime_watcher` settings
   flag in `test_marketops.py`; nothing reaches `app.realtime.book` or
   `app.realtime.collector`.

They are all the same family — `source_backed`, `stale_provider_warning`,
`market_freshness_measured`, `market_quote_age_s` — which is exactly the
rotating-member behaviour already recorded for
`test_live_market_001::TestEndToEnd` under load.

### 5.2 The live control

The same instruments, channels and duration, once with the shipped collector and
once with the fixed one:

| | BEFORE | AFTER |
|---|---:|---:|
| trade frames | 218 | 203 |
| **sequence faults** | **219** | **0** |
| recoveries requested | 1 | 0 |
| `error` frames | 1 | 0 |
| commands sent | 2 | 1 |
| trade sid `seq` | 1…219, 0 gaps | 1…203, 0 gaps |

---

## 6. What this does NOT settle

1. **Whether production behaves the same.** Every number here is DEMO. Nothing
   licenses a claim about production; CP10's separate Tier-2 approval is
   unaffected.
2. **Whether `get_snapshot` recovers a real gap.** It was proven to be *accepted*
   and *answered* on the orderbook sid, from a healthy state. Forcing an actual
   loss and observing the repair cannot be done read-only, and remains untested
   against the venue.
3. **The `ticker` channel has no drop detector.** 2,071 of 2,071 ticker frames
   carried no `seq`. Nothing in this change can fix that — there is no ordering
   field to check — but any statistic derived from ticker frames is derived from
   a stream whose losses are, and will remain, unobservable. That is a
   measurement-contract fact for `MARKET-MICROSTRUCTURE-EDGE-001`, not a bug.
4. **Whether the collector's fault counter is now *complete*.** It is proven not
   to fire falsely and proven to fire on injected gaps and regressions. No live
   gap has ever been observed on any DEMO subscription, so the live positive
   control for the orderbook path is still outstanding.
5. **Whether `error` frames on the orderbook sid consume a seq there too.** The
   2026-08-08 capture says yes (`sid 4, seq 4`, between deltas at 3 and 5) and
   this milestone did not re-observe it, because the fix removed the command
   that was producing errors.

---

## 7. Effect on the standing bar

`KALSHI-CP6-CP9-QUALIFICATION-PREREGISTRATION.md` §3 says CP7 must prove that a
generation boundary drops and re-acquires per-market publishability. §9.1 of the
capacity finding warned that CP7 would be reading a counter already pinned high
for an unrelated reason. **That blocker is cleared**: on a venue with trade flow
the collector now reports zero faults when there are none, so the CP7 counter
means what CP7 needs it to mean.

The frame-floor question the capacity milestone raised is untouched by this work
and still awaits Eric's decision.

## 8. Safety

Read-only. Market-data channels only, all inside `kalshi.ALLOWED_CHANNELS` and
checked by `assert_channels_allowed` at `CollectorConfig` construction. The only
commands that reached a socket were `subscribe` and `get_snapshot`, both built
by `kalshi.build_*` and re-validated by the transport's `assert_sendable`;
`commands_refused = 0` throughout. Nothing was archived — every session ran
`dry_run=True` with `archive_root=None`. No order, position, wallet or
key-management surface was reached. No key material, key id or signed URL was
copied, printed or logged; the run artifacts record a key-id fingerprint and
nothing else. `app/realtime/kalshi.py` is unmodified — the wire confirmed every
builder it defines, including that `market_tickers` is required and that
`taker_side` is the venue's own field. EVO's production checkout stayed clean at
`f100c84` and was verified before and after every run; probe scripts and the
fixed tree lived in throwaway `/tmp` directories.
