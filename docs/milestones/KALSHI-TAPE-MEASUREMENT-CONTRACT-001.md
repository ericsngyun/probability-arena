# KALSHI-TAPE-MEASUREMENT-CONTRACT-001 (P3)

**The durable measurement contract for the Kalshi live tape.**
Branch `KALSHI-TAPE-MEASUREMENT-CONTRACT`. Not merged.

> **AMENDED 2026-08-20 with MEASURED PRODUCTION FACTS.** The contract below was
> written entirely from DEMO evidence. It has since been checked against the
> first production capture — session `s-20260820T003520Z-f450f75ed1fc`, **84,170
> records / 7 segments / 599.6 s** on a 12-market, three-channel universe. Read
> **§0.1** for what changed, **§16** for the production-measured quantities and
> the **universe-selection rule**, and **§17** for the changelog. Where a
> DEMO-derived property is now confirmed on production it is marked *confirmed
> on production* and the DEMO provenance is **kept** — two venues agreeing is
> itself a measurement. Where production **differs**, the difference is stated.

This is prerequisite 3 of `KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001` — *"tape
schema frozen and reviewed as a measurement contract"*. It defines, for every
important raw field, normalized field, counter, channel and reconstructed state,
**what the quantity is, what can be known about it, and what may never be
claimed from it.**

It is a contract, not an implementation milestone. Where it identifies an API
that emits a plausible benign number for something unmeasurable, it **names the
candidate and justifies the change** (§9); it does not make broad API changes.
Three narrowly-required fixes are made and are argued individually in §10; a
fourth defect it found is reported with its remedy rather than patched, and §10.4
says why.

---

## 0. VERDICT

```
P3 measurement contract                       COMPLETE

KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001
  CAPTURE  (read-only production tape)        GO, conditional on B1, B4  (B2 struck)
  REPLAY-EQUALITY VERDICT                     NO-GO until B3 is closed
                                              -> B3 CLOSED 2026-08-19,
                                                 KALSHI-P4-1-REPLAY-REQUAL
```

> **AMENDMENT 2026-08-20.** Capture **happened**, under that conditional GO.
> **B1 is now CLOSED on measurement** (production handshake + read-only
> credential — §11 B1) and **B4 was closed operationally** by the run rule this
> contract specified: one archive root, one session
> (`~/kalshi-prod-tape/p4-attempt2-20260820T003519Z`), no schema bump. The P4
> capture returned **Production semantics QUALIFIED** and **Capture integrity
> QUALIFIED**. The replay-equality line is unchanged by this amendment and is
> not this amendment's to change.

**One sentence, if you read nothing else:** *proceed to P4, but B3 must be
closed before P4 computes a replay-equality verdict — and it is a ~4-line fix
plus a CP8 re-run, not a research problem.*

> **AMENDMENT 2026-08-19 — B3 IS CLOSED.** `KALSHI-P4-1-REPLAY-REQUAL` applied
> the §10.4 remedy and re-ran the replay qualification. The estimate above held:
> the change is confined to `replay()`'s skip branch. **The severity estimate did
> not.** §11 B3 said "live 0 faults, replay 1 fault"; measured on a real
> digest-chained archive that actually contains the frame, it is **live 0 faults,
> replay 12** — the phantom gap halts the subscription and every subsequent delta
> is then refused as unhealthy, so B3 costs the remainder of the tape rather than
> one record. §11 B3 below is amended in place with its outcome; the paragraphs
> that state the defect in the present tense are preserved as the record of what
> was found, and marked. **B1 and B4 are untouched by that work and remain open.**

**This is not one verdict, because P4 is not one act.** P4 captures a production
tape and then computes a CP9-style qualification verdict over it. Those have
different blockers, and collapsing them would be the same mistake CP8's
`checks.state_equality` bit made.

**GO for capture.** The tape's *semantics* are sound. Every quantity has a
provenance, a reconstructability class and a stated limitation; every limitation
that could be mistaken for a measurement is either typed at the API or listed in
§12. Nothing found here damages a captured record: the durable evidence is
complete, digest-chained and re-replayable, so a defect in a pure offline
function can be fixed **after** a capture and re-run against the same bytes.

**NO-GO for the replay-equality verdict until B3 is closed.** `archive.replay()`
skips non-orderbook frames *before* dispatch and therefore does not consume the
sequence number an `error` frame occupies on the orderbook sid — a shape the
2026-08-08 DEMO capture proves the venue produces. **Replay then manufactures a
sequence gap that never happened.** Demonstrated in §11 B3: live 0 faults and
publishable, replay 1 fault and `book_halted`, from the same four records. P4's
qualification asserts replay equality; this construct breaks it, and no
operational workaround exists because the trigger is a venue frame.

Four blockers, three of them outside P3's scope. **One of them, B3, is a genuine
semantic defect found by this milestone** and is deliberately *reported with its
remedy rather than patched* — see §10.3 for why.

**P3 stops here.** No production observation is begun by this milestone. *(The
production observation reported throughout this amendment was begun by P4, not
by P3. P3 opened no socket; see §15.)*

---

## 0.1 PRODUCTION AMENDMENT — what the first production capture changed

Evidence: `docs/milestones/KALSHI-PROD-QUAL-CAPTURE-2-FINDINGS.md` and
`docs/evidence/KALSHI-PROD-QUAL-CAPTURE-2-*.json`. Session
`s-20260820T003520Z-f450f75ed1fc`, 00:36:00.199Z → 00:46:01.437Z,
**84,170 records, 7 segments (all closed), 599.643 s, 12 markets, 3 channels**,
archive verdict `VALID`, `truncated_records 0`, `sequence_faults 0`.

**CONFIRMED ON PRODUCTION** (DEMO provenance retained — §3, §5):

| property | production evidence |
|---|---|
| `orderbook` is **independently sequenced** | 79,256 records, `seq` on 79,256/79,256, contiguous **1 → 79,256**, all fault counters 0 |
| `trade` is **independently sequenced** | 2,516 records, `seq` on 2,516/2,516, contiguous **1 → 2,516**, all fault counters 0 |
| `ticker` is **UNSEQUENCED** | **0 of 2,395** records carry a `seq`. L1 unchanged |
| sid ↔ channel from ack order, ack carries no top-level `sid` | orderbook 1 / ticker 2 / trade 3; three acks, top-level `sid` absent on all three (§3.1) |
| **snapshot ladder typing** reproduced | 13 snapshots: **10** `PRESENT/PRESENT`, **1** `NOT_PROVIDED/PRESENT`, **2** `NOT_PROVIDED/NOT_PROVIDED` |
| **`EMPTY` is still NOT OBSERVED** | 0 of 13 production snapshots, on top of 0 of 360 DEMO snapshots. **L4 stays open** |
| the `use_yes_price` / no-complement convention | 0 locked-or-crossed samples in 2,405 spread samples |

**DIFFERS FROM DEMO** (§16):

| | DEMO | **PRODUCTION** |
|---|---|---|
| mean frame rate, same-size 12-market universe | **0.75 f/s** | **~140 f/s — ~187×** |
| observed 1-second peak | never reached | **565 f/s** |
| rotations | **0**, ever | **6** in 10 minutes |

**THE SIZING PRIOR IS SUPERSEDED, AND IT WAS LOW, NOT CONSERVATIVE.**
`~500 events/s` is no longer an assumption; the measured 1-second peak is
**565 f/s, 13% ABOVE it**. See §16.2 — and note that §16.2 also **corrects a
figure in the P4 findings document**, which reported 485 f/s.

**THE UNIVERSE-SELECTION FINDING — read §16.3 before designing any
microstructure study.** Spearman(trading rate, wire frame rate) **≈ 0.52** on
n=12; the market ranked **last** by trading rate produced the **4th-most** wire
frames, and a medium-stratum market produced **one frame in 600 s**. Therefore:

> **Do not select the microstructure universe using trading volume/activity as a
> proxy for message activity. Our own production measurement says that
> relationship is too weak.**

**STILL UNOBSERVED AFTER PRODUCTION** — none of these is softened by this
amendment: the `EMPTY` ladder (L4), `error`-frame behaviour on production (L8 —
**zero** error frames arrived), venue-initiated disconnect (L7 — **zero**
disconnects), the delta-refusal path (L5), scaling past 12 markets, and **any
hour but this one**.

**One ten-minute overnight capture is not a peak-capacity estimate.** Every rate
figure in this amendment carries that limit (§16.1).

---

## 1. Scope, and the two things called "replay"

The distinction is drawn once, explicitly, because the rest of the document
depends on it.

| | **book replay** | **tape replay** |
|---|---|---|
| question | *what did the venue's order book look like at instant t?* | *reproduce every analysis derivable from the durable raw stream* |
| input | orderbook-sid records | every record, every sid |
| implemented by | `archive.replay()` | nothing |
| status | **SHIPPED, qualified** (CP8: 180/180 market-sessions, bit-identical across two runs) | **NOT BUILT, deliberately** |

`archive.replay()` returns early on every `event_type` outside
`{orderbook_snapshot, orderbook_delta}` (`app/realtime/archive.py:1177-1179`).
It therefore builds a router for the orderbook sid only and reported **one**
subscription where the live lane held **three**.

**The tape is not the limitation.** CP8 §4.6 established this by construction:
`subscription_findings_from_tape` re-derives every sid from the same durable
records with the same `SubscriptionRouter` and reproduces the live ordering
findings exactly, `trade` included (`accepted: 115` on sid 3 in `s2-reconnect`,
matching live). The scope limit is in the function, not in the evidence.

### 1.1 Does a named downstream experiment require tape replay? — NO, TODAY

Searched: `docs/milestones/`, `docs/experiments/`. **`MARKET-MICROSTRUCTURE-EDGE-001`
does not exist as a document.** It is named as a *future* programme in
`AGENTS.md` and in the P4 milestone, and nothing preregistered today reads a
`trade` or `ticker` record on replay.

**Decision: leave tape replay unbuilt.** Building it now would be aesthetics.
The boundary is instead documented — which is exactly option 3 of the CP6–CP9
report's §9 recommendation ("*consider widening `archive.replay()` … or
documenting in the schema freeze that it is orderbook-only*") — and pinned by a
test so that widening it later retires this note on evidence.

### 1.2 The trigger that makes tape replay REQUIRED

Recorded now, so the decision is not re-litigated from memory:

> Tape replay becomes a **prerequisite** the moment a preregistered experiment
> declares a feature family that reads a `trade` or `ticker` record — signed
> order flow, adverse selection, trade-through, realized/effective spread,
> rolling ticker volume, quote-update intensity. The P4 milestone's own
> motivation paragraph already names *adverse selection*, which is a trade-print
> feature. **Nothing may consume `trade` on replay until `archive.replay()`
> covers it.**

Until then, `replay()`'s output describes the orderbook sid and says so.

---

## 2. How to read the contract tables

Every quantity carries the fourteen attributes the milestone requires. Six of
them are **properties of the channel, not of the field**, and are stated once in
§3 rather than repeated eighty times:

- (4) channel and SID semantics
- (5) sequence domain
- (6) ordering guarantees
- (7) whether packet/frame loss is detectable
- (8) whether source completeness is knowable
- (9) subscription-generation semantics

Every field row in §5–§8 names its channel and **inherits** those six. The
remaining eight — provenance, wire representation, normalized representation,
units/precision, missing-value semantics, reconstructability, current replay
support, positive control, known limitations — are per-field and are in the
tables.

**Provenance vocabulary** (fixed, three values):

| | means |
|---|---|
| `VENUE_FACT` | the venue said this. It is on the wire, verbatim, and is conserved byte-for-byte in `raw_event`. |
| `COLLECTOR_FACT` | *we* did this or observed this at capture. The venue never said it and it cannot be re-derived from venue bytes. |
| `DERIVED_STATE` | computed from venue facts by a pure function. Reproducible from the tape by re-running that function. |

**Reconstructability vocabulary** (fixed, four values):

| | means |
|---|---|
| `RAW_REPLAYABLE` | the exact venue bytes are on the tape; any future reading is possible. |
| `DERIVABLE_FROM_RAW` | not stored as such, but a pure function of stored raw. |
| `LIVE_ONLY` | knowable only at capture; it **is** on the tape because it was stamped there, and would be unrecoverable otherwise. |
| `NOT_RECONSTRUCTABLE_BY_DESIGN` | a collector action the tape does not record. A replay reader gets a **wrong** value, not a missing one, unless it is told. |

---

## 3. THE CHANNEL CONTRACT

The load-bearing table. Everything else inherits from it.

Established on four independent sockets: the P0 wire capture
(`p0-wire-test-instruments-60`, 8,179 frames) and CP6–CP9's `s1-observe` /
`s2-reconnect` / `s3-drop` (24,396 frames), 2026-08-17, DEMO.

**Confirmed on production 2026-08-20** on a fifth, independent socket — session
`s-20260820T003520Z-f450f75ed1fc`, **84,170 frames** over 599.6 s, three
channels, 12 markets. The DEMO basis is **retained, not replaced**: this table
was derived from DEMO and is now known to hold on both venues, which is a
stronger statement than either alone. Production census, generation-blind:

| sid | channel | frames | `seq` present | first → last | contiguous | gaps | dups | regressions |
|---|---|---:|---:|---|---:|---:|---:|---:|
| 1 | `orderbook_delta` (+ 13 snapshots) | 79,256 | **79,256 / 79,256** | **1 → 79,256** | 79,255 | **0** | 0 | 0 |
| 2 | `ticker` | 2,395 | **0 / 2,395** | — | 0 | *empty domain* | 0 | 0 |
| 3 | `trade` | 2,516 | **2,516 / 2,516** | **1 → 2,516** | 2,515 | **0** | 0 | 0 |
| — | `subscribed` | 3 | 0 | — | — | — | — | — |

The collector's own **generation-aware** counters agree on every sid, and every
one of `gaps / duplicates / regressions / wrong_sid / stale_generation /
missing_seq / recoveries / generation_advances` is **0**, under a single
`subscription_generation = 1` with 0 disconnects and 0 reconnects.

Two rows of the table above were **not exercised** in production and are stated
as such, not as passes: the `error` control frame (**zero arrived in 600 s** —
§3.3, L8) and the repair path (`get_snapshot`/`update_subscription` — **never
needed**, `commands_refused: 0`, one outbound `subscribe` and nothing else).

| | `orderbook_delta` | `ticker` | `trade` | control (`subscribed`, `error`, `ok`) |
|---|---|---|---|---|
| **sid in the 3-channel subscribe** (assignment-dependent, **not** a constant — §3.1) | **1** | **2** | **3** | `subscribed`: none; `error`: the sid it answers |
| **event types carried** | `orderbook_snapshot`, `orderbook_delta` | `ticker` | `trade` | `subscribed`, `error` |
| **sequence domain** | `(session, subscription_generation, sid)` | **NONE — an empty domain** | `(session, subscription_generation, sid)` | shares the answered sid's domain |
| **`seq` present** | 5,886/5,886 · 7,311/7,311 | **0 / 2,071** · **0 / 1,852** · **0 / 1,694** · **0 / 1,454** | 219/219 · 143/143 | `subscribed`: no seq **and no sid**; `error`: **yes, and it consumes one** |
| **ordering guarantee** | `seq_{n+1} = seq_n + 1` within a generation | **none. Arrival order only, and arrival order is not venue order.** | `seq_{n+1} = seq_n + 1` within a generation | n/a |
| **loss detectable?** | **YES** — a hole in `seq` | **NO. Not now, not by any amount of further work on this tape.** | **YES** — a hole in `seq` | via the sid it consumes from |
| **completeness knowable?** | **YES**, within a generation | **NO** | **YES**, within a generation | n/a |
| **repair available?** | **YES** — `get_snapshot`/`update_subscription`, accepted and answered on this sid | **NO** | **NO.** A lost print is lost. | n/a |
| **generation semantics** | `seq` **restarts at 1** each generation; the boundary EXPLAINS the discontinuity | stamped per record; the per-subscription epoch counter is **not** derivable (§8.2) | `seq` **restarts at 1** each generation | n/a |
| **default-on?** | yes (`DEFAULT_CHANNELS`) | yes | **NO** — operator must name it | implicit |
| **`replay()` covers?** | **YES** | no | no | no |

### 3.1 A sid is NOT a per-channel constant

The table above records the sid assignment **observed under one particular
subscribe**, and it is a property of that subscribe, not of the venue.

| capture | channels named | sid assignment |
|---|---|---|
| P0 / CP6–CP9 (2026-08-17, DEMO) | `orderbook_delta`, `ticker`, `trade` | orderbook **1**, ticker **2**, trade **3** |
| DEMO wire (2026-08-08) | four channels | ticker **1**, lifecycle **2**, trade **3**, orderbook **4** |
| **PRODUCTION (2026-08-20)** | `orderbook_delta`, `ticker`, `trade` | orderbook **1**, ticker **2**, trade **3** |

**Confirmed on production, and confirmed in the way that matters.** The
production acks assigned sids in ack order for the channels the subscribe named,
and the ack order matched the request order — so the *same* subscribe produced
the *same* assignment on a different venue. That **confirms §3.1 rather than
retiring it**: it is still a property of the subscribe, and nothing here
licenses hard-coding `sid == 1 -> orderbook`. All three production acks again
carried **no top-level `sid`**, so they remain unroutable; the collector
discovered the orderbook sid from frames (`carries_orderbook` true on sid 1
only), exactly as this section requires.

**The venue assigns sids in ack order for the channels that subscribe names.**
Change the channel list and every sid moves. Nothing may hard-code
`sid == 1 -> orderbook`; the mapping is discovered from **frames**
(`SubscriptionState.carries_orderbook`, set on the first orderbook frame routed
through) and deliberately **not** from the subscribe command, because the live
lane and the replay lane both see frames and only the live lane ever saw the
command — so both reach the same verdict.

The `subscribed` ack does name the channel, but it carries **no top-level
`sid`** (the sid is inside `msg`), so it is not routable to a subscription and
cannot serve as the source.

This also matters for §11 B3: the orderbook-sid `error` frame on the wire was
`sid 4`, because on that day the orderbook channel was sid 4.

### 3.2 The three facts that must never be forgotten

1. **Sequence identity is `(session/connection, subscription_generation, sid)`.**
   It is not market-level. Assuming `seq = f(market)` produced 219 false faults
   on a stream that ran 1..219 perfectly clean. A generation-blind per-sid census
   of `s2-reconnect` reports **999 apparent regressions and 2 apparent duplicates
   on sid 1, and 9 apparent regressions on sid 3**; the generation-aware lanes —
   live and replay — both report **zero**. The epoch is load-bearing, not
   decorative: the venue **re-issues the same sids 1/2/3 on every resubscribe**,
   so sid alone cannot separate epochs.

2. **Orderbook and trade are independently sequenced.** The `s3-drop` gap on sid
   1 unpublished all 60 books on sid 1 and left sid 3 untouched — `trade` ran
   1…80 with zero gaps straight through the event.

   **Confirmed on production, at scale and without an injected fault.** Over the
   same wall clock, sid 1 ran **1 → 79,256** and sid 3 ran **1 → 2,516**, each
   perfectly contiguous from 1. A shared sequence space would have manufactured
   tens of thousands of faults; the observed count is **0**. DEMO proved this by
   forcing a gap on one sid and watching the other survive; production proves it
   by two independent counters running 31× apart in the healthy case. **Two
   different experiments, same conclusion.**

3. **`sequence_gaps = 0` on `ticker` is an arithmetic artefact of an empty
   domain, not an observation.** Any feature derived from `ticker` — a rolling
   volume, a quote-update rate, anything — inherits *"no sequence-based loss
   detection"* from its source and **may never be described as lossless.** A
   drift detector (`test_the_ticker_channel_still_carries_no_sequence_number`)
   retires this caveat on evidence if the venue ever starts sequencing ticker.

   **Confirmed on production: 0 of 2,395 ticker records carry a `seq`.** The
   empty domain is a property of the *venue's* ticker channel, not of DEMO. The
   production run reported `sequence_gaps = 0` and `missing_seq = 0` on sid 2
   and typed **both** — `missing_seq` is unreachable there for the same reason
   (`dispatch` passes over frames carrying no `seq` at all), so it too is
   arithmetic rather than an observation. **L1 is unchanged and is now a
   two-venue fact.**

### 3.3 Control frames consume sequence numbers

Confirmed on the wire: `{"type":"error","sid":3,"seq":2,...}` arrived as the
219th frame on a stream of 218 trades, and a 2026-08-08 capture shows
`{"type":"error","sid":4,"seq":4}` between deltas at seq 3 and seq 5.
`SubscriptionRouter.dispatch` therefore consumes a sequence number for **every**
frame carrying one, with `needs_base=False`, and passes over frames with no
`seq` at all. Skipping a control frame's `seq` would manufacture a gap on the
next real one.

**Not settled:** whether `error` consumes a `seq` on the *orderbook* sid was not
re-observed in P0, because the fix removed the command that was producing errors.
The 2026-08-08 capture says yes. Listed in §12.

**Production did NOT settle it either, and this amendment does not soften it.**
The 2026-08-20 session received **zero `error` frames in 600 s**, so the run
neither confirms nor refutes the rule. Recorded as *not re-observed*, never as a
pass. The conservative assumption in the code (an `error` does consume one)
stands untouched — assuming *no* would manufacture gaps, assuming *yes* cannot
hide one. **L8 remains OPEN after production.**

---

## 4. THE TAPE'S DURABLE SHAPE

Three nested layers. Knowing which layer a quantity lives in is most of the
contract.

```
segment record  (RECORD_FIELDS, 16 digest-bound + record_digest)
  └── raw_event          = the venue frame, VERBATIM               VENUE_FACT
  └── normalized_event   = EventEnvelope minus `raw`
        └── normalized   = normalize_frame() output (observation)  DERIVED_STATE
```

`raw` is stored **once**. `read_verified()` grafts `raw_event` back onto
`normalized_event` at read time, so a replay reader sees a whole `EventEnvelope`
(`app/realtime/archive.py:969-982`).

### 4.1 Record envelope (`segment.RECORD_FIELDS`)

Closed and digest-bound: `record_digest = SHA-256(canonical_bytes(...))` over
exactly these sixteen fields. An undeclared top-level key is **refused**, not
tolerated — an unknown key would ride entirely outside the digest.

| field | provenance | units / precision | missing-value semantics | reconstructability |
|---|---|---|---|---|
| `schema_version` | `COLLECTOR_FACT` | int, pinned `1` | refused if ≠ 1 | `LIVE_ONLY` |
| `canonical_schema_version` | `COLLECTOR_FACT` | int, pinned `1` | refused if ≠ 1 — digests are comparable only within one encoding version | `LIVE_ONLY` |
| `environment` | `COLLECTOR_FACT` | enum `demo`\|`production` | refused if unknown. `append()` refuses a demo envelope into a production archive | `LIVE_ONLY` |
| `segment_id` | `COLLECTOR_FACT` | str, `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$` | required | `LIVE_ONLY` |
| `connection_generation` | `COLLECTOR_FACT` | int ≥ 1, or `None` = **UNKNOWN** | **`None`, never 0.** 0 would be a fabricated epoch | `LIVE_ONLY` |
| `subscription_generation` | `COLLECTOR_FACT` | int ≥ 1, or `None` = **UNKNOWN** | **`None`, never 0** | `LIVE_ONLY` |
| `subscription_id` (= wire `sid`) | `VENUE_FACT` | int | `None` when the frame carries none (`subscribed` acks) | `RAW_REPLAYABLE` |
| `receive_ordinal` | `COLLECTOR_FACT` | int, monotone within a segment | required | `LIVE_ONLY` |
| `message_type` (= wire `type`) | `VENUE_FACT` | str; `"unknown"` when absent | `"unknown"` — the frame is still archived | `RAW_REPLAYABLE` |
| `market_ticker` | `VENUE_FACT` | str | `None` for `subscribed`/`error` and any frame carrying none | `RAW_REPLAYABLE` |
| `seq` | `VENUE_FACT` | int | **`None` = the venue sent none.** Never 0 | `RAW_REPLAYABLE` |
| `received_at_utc` | `COLLECTOR_FACT` | RFC3339 UTC, **exactly 6 fractional digits** | required | `LIVE_ONLY` |
| `received_monotonic_ns` | `COLLECTOR_FACT` | int ns, `time.monotonic_ns()`. **Durations only** — meaningless across processes | required | `LIVE_ONLY` |
| `raw_event` | `VENUE_FACT` | opaque; floats coerced to `Decimal` via `coerce_canonical` (lossless: `Decimal(repr(f))`) | required | `RAW_REPLAYABLE` |
| `normalized_event` | `DERIVED_STATE` | `EventEnvelope` minus `raw` | required | `DERIVABLE_FROM_RAW` |
| `previous_record_digest` | `COLLECTOR_FACT` | 64-hex; the chain link | required | `LIVE_ONLY` |

**Precision contract, stated once.** `float` is *refused* by the canonical
encoder, everywhere. Prices, sizes and durations are integers or `Decimal`;
`data_age_us` and `time_to_resolution_us` are **integer microseconds**. This is
not stylistic — a bare float written and re-read as `Decimal` re-serialises
differently, which once made every record carrying a venue timestamp fail its own
digest and vanish on read.

**What the envelope does NOT carry, and it matters:** there is **no session
identity**. Not a `session_id`, and not derivable — `connection_generation`
restarts per session and `segment_id` is `venue.YYYY-MM-DDTHH[.rNNNN]`, a
wall-clock partition plus a rotation counter. See §11 B4.

### 4.2 Integrity, ordering and durability

| property | mechanism |
|---|---|
| record self-integrity | `record_digest = digest_hex({k: record[k] for k in RECORD_FIELDS})` — a field added later cannot silently fall outside the digest |
| record **order** | `previous_record_digest` chain, seeded by `genesis_digest = "genesis:" + digest_hex({schema_version, segment_id, environment})`. A constant sentinel would let record #1 of one segment splice into another segment's head and still chain |
| segment order | `ordered_stream_digest = fold(previous, record_digest)` — two records that swap places produce the same *set* of self-digests and a different fold |
| segment identity | `manifest.json`: `record_count`, `first`/`last_record_digest`, `ordered_stream_digest`, `event_file_sha256`, `event_file_size_bytes`, `previous_segment_digest`, `close_status`, `partition_identity`, `writer_version` |
| archive order | the **committed head's generation chain**, not directory order. `read_verified()` refuses if the history cannot be resolved; `read_unverified_diagnostic()` falls back to directory order and sets `diagnostic_order_unauthenticated` |
| acceptance | `submit()` canonicalises and appends on the **caller's** thread under one lock — *a caller is never told ACCEPTED before the canonical writer owns the event* |
| durability | flush cadence `flush_every = 256`; manifest written to a temp file, fsynced, atomically renamed, directory fsynced. **Rename-after-fsync is the durability contract, not `close()`** |
| commit | `close()`. Until it runs there is **no authoritative record count and an unclosed segment is explicitly not evidence** |
| environment isolation | `append()` refuses a `demo` envelope into a `production` archive — *demo events must never become production evidence* |
| creation | an archive must **already exist**; a collector consumes one and can never bring one into existence. Operator step: `archive-init --confirm` |

Reads that drop records **count** them rather than omitting silently:
`truncated_records` (could not be decoded), `tampered_records` (decoded, chain
broken), `foreign_environment_records`, `missing_committed_segments`. *"Nothing
was lost" must not be asserted by omission.*

**Archive order equals wire order only under a single producer per
subscription** — which the collector structurally is: one task, one socket, one
synchronous `append()` on its own stack.

---

## 5. VENUE FACTS — the wire fields, per channel

`raw_event` is verbatim, so **every one of these is `RAW_REPLAYABLE`** and every
one inherits its channel row from §3. The table records the wire representation,
the normalized representation, units, and the missing-value semantics.

The sid numbers in the headings below are the **three-channel subscribe's**
assignment and are not venue constants — see §3.1.

### 5.1 `orderbook_snapshot` / `orderbook_delta` (sid 1)

| wire field | normalized as | units / precision | missing-value semantics |
|---|---|---|---|
| `market_ticker` | `observation.market_ticker`; `coverage[market]` | str | `coverage[market] = absent:not_supplied_by_venue` |
| `market_id` | `EventEnvelope.market_id` | str | `None` |
| `yes_dollars_fp` | `book.bid_levels[]`, `LADDER_SUPPLIED`/`LADDER_OMITTED` | `[["0.4700","5.00"], …]` → price units 1/10 000 $, size units 1/100 contract | **`absent:not_supplied_by_venue`, and `venue_omitted_bid_ladder = true`.** A JSON `null` counts as absent — a null is the venue declining to answer |
| `no_dollars_fp` | `book.ask_levels[]` (**identity, YES-scaled**) | as above | as above, `venue_omitted_ask_ladder` |
| `side` (delta) | `book.changed_level.venue_side` | `"yes"`\|`"no"` | `absent:not_supplied_by_venue` |
| `price_dollars` (delta) | `changed_level.raw_price_units` | 0–4 dp, 1/10 000 $ | delta refused; book **halts** |
| `delta_fp` (delta) | `changed_level.delta_contract_units` | 0–2 dp, **signed** | delta refused; book **halts** |
| `ts_ms` | `EventEnvelope.venue_time` | epoch **milliseconds** | falls back to `time`/`ts`/`timestamp` |
| `ts` | fallback | **ISO-8601 string on this channel** | — |

**The `use_yes_price=true` contract, confirmed on the wire and load-bearing.**
Both ladders arrive on the YES price scale. **The NO-side price IS the YES ask
and no complement is applied.** Ground truth: ticker `yes_bid 0.4700 / yes_ask
0.5100` against book `yes_dollars_fp [["0.4700","5.00"]]`, `no_dollars_fp
[["0.5100","206.00"]]`. The code previously complemented (`1 − 0.5100 = 0.4900`)
and would have reported an ask two cents below the real one — uncrossed,
plausible, wrong. The convention is recorded **on the data**
(`use_yes_price_requested`, `no_side_normalization: "identity_yes_scaled"`), not
only in a docstring, and `_require_uncrossed` is the cheap invariant that catches
its reversal.

**Confirmed on production.** Across **2,405** spread samples (10 full-ladder +
2,395 top-of-book) there were **0 locked-or-crossed** samples. Had the NO side
needed complementing, crossed books would have appeared at that sample size.
The two distributions are reported separately and never pooled — full-ladder
median $0.0100 (n=10); top-of-book median $0.0100, max $1.0000 (n=2,395) — and
no absence became a zero (§6.2).

**Ladder presence is typed and is not a level count.**
`NOT_PROVIDED != EMPTY != PRESENT`, carried beside every level count in
`top_of_book()`, `yes_scale_ladder()` and the snapshot result. Measured over 360
live snapshots:

| | s1 (60) | s2 (180) | s3 (120) | P0 (60) | **PROD (13)** |
|---|---:|---:|---:|---:|---:|
| both ladders present | 12 | 171 | 96 | 57 | **10** |
| one side's key absent | 45 | 0 | 18 | 0 | **1** |
| **both** absent | 3 | 9 | 6 | 3 | **2** |
| a side present but **EMPTY** | **0** | **0** | **0** | **0** | **0** |

48 of `s1`'s 60 snapshots omitted at least one side. **The case that used to be
collapsed into "present with zero levels" is the common case, not an edge case.**
`EMPTY` has **never been observed live** (§12).

**Reproduced on production, and it is load-bearing there on real money.** 3 of
13 production snapshots omitted at least one ladder (1 `NOT_PROVIDED/PRESENT`,
2 `NOT_PROVIDED/NOT_PROVIDED`). **Two production books terminated
`publishable: true` with `levels_yes = levels_no = 0`**, and the only field
separating that from a genuinely empty book is `ladder_presence =
omitted_by_venue`, which was present and correct on both. §7.1's rule — *"a
zero-level book is never observed emptiness unless `ladder_presence` says so"* —
would have been violated on **2 of 12 production markets** by a consumer reading
`levels_*` alone.

**`EMPTY` is still NOT OBSERVED — 0 of 13 on production, on top of 0 of 360 on
DEMO.** n = 13 is small and this is weak evidence. It is **not** a retirement of
the `NOT_PROVIDED != EMPTY != PRESENT` distinction, and **L4 stays open.**

### 5.2 `ticker` (sid 2) — **UNSEQUENCED**

| wire field | normalized as | units | missing-value semantics |
|---|---|---|---|
| `yes_bid_dollars` \| `yes_bid` | `quote.bid_levels[0]` | 1/10 000 $ | `coverage[bid_levels] = absent:not_supplied_by_venue` |
| `yes_ask_dollars` \| `yes_ask` | `quote.ask_levels[0]` — **identity, already YES-scaled** | 1/10 000 $ | as above |
| `yes_bid_size_fp` \| `yes_bid_size` | `bid_levels[0].size` | 1/100 contract | `size: null` |
| `yes_ask_size_fp` \| `yes_ask_size` | `ask_levels[0].size` | 1/100 contract | `size: null` |
| `price_dollars` \| `price` \| `last_price_dollars` | `quote.last_price` | 1/10 000 $ | `None` |
| `ts` | venue time — **epoch SECONDS on this channel** | s | — |
| `ts_ms` | venue time, preferred | ms | — |

**`ts` means different things on different channels.** `orderbook_delta` sends
an ISO string; `ticker` sends epoch seconds. `ts_ms` is read **first** because it
is unambiguous wherever it appears. The old `int(ts)` produced a 1970 date for
the ISO form and a 1000× inflated age for the seconds form.

**`quote.depth = "top_of_book_only"` is asserted on the data.** A ticker frame
is not a book and nothing may read it as one.

### 5.3 `trade` (sid 3) — off by default

| wire field | normalized as | units | missing-value semantics |
|---|---|---|---|
| `taker_side` | `trade.direction.normalized_taker_side` | enum `yes`\|`no` | **`None`, `source` names the absence, `coverage[trade_direction]` records it** |
| `yes_price_dollars` \| `yes_price` | `trade.yes_price` | 1/10 000 $ | `absent:not_supplied_by_venue` |
| `no_price_dollars` \| `no_price` | `trade.no_price` — **no complement applied** | 1/10 000 $ | `None` |
| `count_fp` \| `count` | `trade.quantity` | 1/100 contract | `absent:not_supplied_by_venue` |
| `trade_id` \| `id` | `trade.trade_id` | opaque str | `venue_field: null` |
| `taker_book_side`, `taker_outcome_side` | retained in `raw_event` only | — | — |

**Trade direction is a venue field and is never inferred.** `_normalize_trade`'s
*only* argument is the venue's message — no book, no quote, no previous print is
in scope, so a Lee-Ready, tick-rule or quote-rule classifier has nothing to
classify against. That signature is the control. Published work puts naive
direction inference from a public book feed at ~59–62% accuracy — enough to flip
the **sign** of effective spread and Kyle's lambda. Every trade record carries
`inference_policy` stating this on the artifact.

`yes_plus_no_price_units` is recorded as an **arithmetic observation**, not an
assumption: if the venue's two prices are complements it equals 10 000. Recorded
so the convention can be checked from the tape rather than asserted.

### 5.4 Which field name supplied a value is itself an observation

Two field-name sets are in play — this repository's wire-confirmed fixtures, and
a third-party mirror of the venue docs marked UNVERIFIED for `trade` and
`market_lifecycle_v2`. Every normalized block therefore records `venue_field`:
*"0.5100 came from `yes_price`"* and *"0.5100 came from `yes_price_dollars`"* are
**different observations about the venue**. The first real session settles the
question from the tape instead of from a document.

**SETTLED on production, for `ticker`.** Production quotes used
`yes_bid_dollars` / `yes_ask_dollars` on **2,395 of 2,395** frames; the bare
`yes_bid` / `yes_ask` spelling **never appeared**. This is a fact about the
production venue at this hour, not a proof that the bare spelling is
unreachable — the alternate names stay in the reader. `trade` and
`market_lifecycle_v2` field naming remains UNVERIFIED against venue docs; the
production `trade` sid carried 2,516 records and the same census should be run
over them before any `trade` field name is used as an experimental variable
(doctrine 8).

---

## 6. DERIVED STATE — the normalized observation

`normalize_frame` is **pure and total**: same frame in, same dict out, for every
frame including types it has never seen. An unrecognised `type` still produces a
full `coverage` map. Provenance `DERIVED_STATE`; reconstructability
`DERIVABLE_FROM_RAW`; replay support: **re-derivable from `raw_event`, and CP8
proved it — 9,309 / 8,599 / 6,488 records re-normalized byte-identically.**

### 6.1 The closed absence vocabulary

The single most important thing in the normalized layer. **Epistemic absence
never becomes a numeric zero where zero has economic meaning.**

| token | means |
|---|---|
| `present` | observed and parsed |
| `absent:not_supplied_by_venue` | the venue could have sent this on this kind of frame and did not |
| `absent:not_applicable_to_this_frame` | this frame kind never carries it (a trade print has no ask ladder) |
| `absent:venue_value_unparseable` | present but refused by the fixed-point contract. **Raw is retained regardless**, so the decision is revisitable |
| `absent:not_derivable_from_this_frame_alone` | derivable only across frames; this normalizer holds no state |
| `absent:exceeds_normalization_bound` | ladder beyond `MAX_NORMALIZED_LEVELS = 40 000`. Raw still archived whole |

Eleven required observations carry one of these on every record:
`timestamp`, `sequence`, `market`, `bid_levels`, `ask_levels`, `trade_price`,
`trade_quantity`, `trade_direction`, `spread`, `market_state`,
`time_to_resolution`.

### 6.2 `spread_units` — the field this vocabulary exists for

| frame kind | value | coverage |
|---|---|---|
| snapshot, both sides present and non-empty | `best_ask − best_bid`, price units | `present` |
| snapshot, one side omitted / empty | **`null`** | `absent:not_supplied_by_venue` |
| snapshot, a side unparseable | **`null`** | `absent:venue_value_unparseable` |
| **delta** | **`null`** | `absent:not_derivable_from_this_frame_alone` |
| ticker, both quotes present | `ask − bid` | `present` |

**Never zero.** A zero spread is a tradable market, which is the opposite of what
any of those absences means.

### 6.3 `depth` — a claim about how much of the book this record describes

`full_ladder` \| `one_side_ladder_only` \| `no_ladder_supplied` \|
`single_level_change` \| `top_of_book_only`. It was once hardcoded to
`"full_ladder"` whatever arrived — *"asserting `full_ladder` on a snapshot that
transmitted no ladder is the same defect stated as a schema field."*

### 6.4 `market_state` and `time_to_resolution`

- An unrecognised state is **retained verbatim and reported unparseable**, never
  mapped onto the nearest familiar word. `known_vocabulary` travels on the record.
- `time_to_resolution_us` is derived from **this frame's own** close stamp or not
  at all. A close time seen on an earlier lifecycle frame is deliberately not
  carried forward: caching it would make a record's contents depend on what the
  collector happened to have seen, so the same fixture could normalize two ways.
  The join belongs to the reader, with the whole session in hand.
- Epoch-second sanity bounds `[1e9, 4e9]` refuse a value in unconfirmed units
  rather than silently producing a date 50 000 years out.

---

## 7. RECONSTRUCTED STATE — the order book

Provenance `DERIVED_STATE`. Replay support: **`archive.replay()`, qualified.**

### 7.1 Publication state is typed, not a boolean

Four causes produce "not publishable" and a consumer that cannot tell them apart
cannot act on any of them:

| state | means | is something wrong? |
|---|---|---|
| `publishable` | synced, sequence-clean, and based in the current epoch | — |
| `book_halted` | integrity is broken — gap, regression, rejection, crossed book, negative level | **YES** |
| `awaiting_snapshot_for_generation` | the subscription moved to a new epoch and **this market** has not yet received **its own** snapshot | **no** |
| `subscription_unhealthy` | the subscription itself awaits a base | no |

Both epochs (`subscription_generation`, `based_generation`) travel on every
`PublicationState`. **Equality of those two IS the invariant.**

**Why per-market.** A sibling's snapshot re-bases the sibling and says nothing
about anyone else's ladder. Measured live: one entry at frame 604 flipped all 60
markets to publishable when **one** had been re-snapshotted; the other 59 were
republished on a sibling's snapshot, still carrying ladders from the epoch the
venue had just abandoned. After the fix, frames 605…663 give 59 separate entries,
one acquisition each.

**A zero-level book is never observed emptiness unless `ladder_presence` says so.**

**This rule fired on production.** Two of twelve production markets terminated
`publishable: true` with zero levels on both sides and
`ladder_presence = omitted_by_venue` (§5.1). The distinction is no longer a
hypothetical defended by fixtures; it is the only thing standing between a
reader and a fabricated empty book on 17% of a real production universe.

The per-market boundary machinery was **not exercised** in production: one
connection, one `subscription_generation`, 0 reconnects, so no book ever entered
`awaiting_snapshot_for_generation` there. That path's evidence remains the CP7
DEMO re-run.

### 7.2 Book quantities

| quantity | units | missing-value semantics | limitation |
|---|---|---|---|
| `best_yes_bid_units` | 1/10 000 $ | `None` when the YES ladder is empty **or** was never transmitted — read `ladder_presence` to tell which | gated: raises `BookIntegrityError` if not publishable |
| `best_yes_ask_units` | 1/10 000 $ | as above, from the **NO** ladder | derived from NO, never assumed to exist on YES |
| `spread_units` | 1/10 000 $ | `None` if either side is `None` | **never 0** |
| `yes_levels` / `no_levels` | count | `0` is ambiguous **alone** | must be read with `ladder_presence` |
| `ladder_presence` | `supplied` \| `omitted_by_venue` \| `no_snapshot_applied` | — | **not in `checksum()`** — see below |
| `checksum()` | 16-hex | `None` in `replay()` output when not publishable | digests `{market_ticker, generation, last_seq, sid, yes, no}` and **NOT `ladder_presence`**, so equal checksums are no evidence a typed absence survived. CP8 compares it separately: 60/60 in every session |
| `generation` | int | — | counts **this book's** resynchronisations — a different thing from `subscription_generation` |

---

## 8. COUNTERS

### 8.1 What each counter is a fact *about*

| counter family | provenance | reconstructability |
|---|---|---|
| `SubscriptionState.stats` — `accepted`, `gaps`, `duplicates`, `regressions`, `wrong_sid`, `stale_generation`, `missing_seq`, `generation_advances` | `DERIVED_STATE` (a property of the **stream**) | `DERIVABLE_FROM_RAW` — verified equal live vs tape on every sequenced sid |
| `SubscriptionState.stats["recoveries"]` | **`COLLECTOR_FACT`** (a record of an **action**) | **`NOT_RECONSTRUCTABLE_BY_DESIGN`** |
| `OrderBook.stats` — `snapshots`, `deltas`, `duplicates`, `gaps`, `regressions`, `rejected_pre_snapshot`, `rejected_pre_generation_snapshot`, `generation_boundaries` | `DERIVED_STATE` | `DERIVABLE_FROM_RAW` — 60/60 stat blocks identical live vs replay |
| `OrderBook.stats["resyncs"]` | `COLLECTOR_FACT`-ish (sid change / explicit mark) | partial |
| `CollectorResult.*` | `COLLECTOR_FACT` | `LIVE_ONLY` |
| `collector_metrics` interval record | `COLLECTOR_FACT` | `LIVE_ONLY` — a separate telemetry file, not the tape |

### 8.2 The two quantities that do NOT reconstruct — stated, not omitted

Both are **records of collector actions, not market state**, and neither affected
a single book. Both are the repo's characteristic failure shape and are recorded
in full.

**(a) `recoveries` — a replay reader gets a wrong number, not a missing one.**

| session | sid | channel | live | from tape |
|---|---:|---|---:|---:|
| `s2-reconnect` | 1 | `orderbook_delta` | 2 | **0** |
| `s2-reconnect` | 2 | `ticker` | 2 | **0** |
| `s2-reconnect` | 3 | `trade` | 2 | **0** |
| `s3-drop` | 1 | `orderbook_delta` | **14** | **0** |

`recoveries` counts `begin_recovery()`. The tape is a record of **inbound venue
messages**; the outbound `update_subscription` is not archived and nothing marks
the moment the collector chose to send it. **A reader replaying `s3-drop` would
conclude the collector resynchronised zero times when it resynchronised
fourteen.** Every other field matched exactly on every sequenced sid.

Note also that two different counters are both called "recoveries" and are not
the same number: `CollectorResult.recoveries_requested` counts **commands
actually sent** (deduped by `_recovery_pending` — `s3-drop`: **1**), while
`SubscriptionState.stats["recoveries"]` counts `begin_recovery()` calls
(`s3-drop`: **14**).

**(b) `generation_advances` on the unsequenced sid.** Sid 2 (`ticker`): live 2,
tape-derived 0, because `dispatch` can only advance an epoch on a frame carrying
a `seq`, and no ticker frame does. **The per-record generation stamps are all
present and all conserved** — a reader can always say which epoch any individual
ticker record arrived under. Only the per-subscription *counter* is lost.

### 8.3 `sequence_faults` is NOT synonymous with packet loss

A hard rule. `CollectorResult.sequence_faults` increments on **every**
`SubscriptionError` and on every book-level refusal:

| cause | is it loss? |
|---|---|
| `sequence_gap` | **probably yes** |
| `sequence_regression` | maybe |
| `wrong_sid` | no — a routing/identity fault |
| `stale_generation` | no — a straggler from an epoch we left |
| `missing_sequence` | no — the venue sent no `seq` |
| `awaiting_snapshot` | no — a cold start |
| `rejected_pre_generation_snapshot` | **no — this is a benign reconnect boundary** |
| `BookIntegrityError` / `FixedPointError` | no — one market's reconstruction |
| a frame with a `seq` but no int `sid` | no |

Only `gap`, `regression` and `duplicate` are forwarded to the metrics lane —
which has a bucket for each — and the collector honours that closure rather than
laundering the rest into a counter that means something else. Everything else is
counted in `CollectorResult.sequence_faults`, **where it is true and where it
must not be read as loss.**

`rejected_pre_generation_snapshot` is counted on its own axis in `OrderBook.stats`
and is **never** merged into `gaps` or `rejected_pre_snapshot`: it is neither loss
nor a cold start, and merging it would make a routine reconnect boundary
indistinguishable from both. It is **deliberately not a `_halt`** — nothing is
broken.

### 8.4 The metrics interval record (`kalshi-live-tape.jsonl`)

A **closed** schema, structurally incapable of carrying a market ticker: exactly
four string fields, each pinned to a format regex; the subscription arrives as
`markets_subscribed: int`. An unknown field is a refusal, not a passthrough.

Selected semantics:

| field | semantics | known limitation |
|---|---|---|
| `events_received/archived/rejected`, `frames_malformed` | per-interval deltas of cumulative counters | conservation holds: `received = archived + rejected + malformed` |
| `event_bytes_total` | delta of `TransportCounters.bytes_received` | **per-frame attribution is approximate**: malformed frames are counted but never yielded, so their bytes fall into the next yielded frame's delta. The aggregate is exact |
| `append_us_max`, `segment_close_ms_max` | producer-owned maxima, zeroed by the flusher | a sample may be attributed to an adjacent interval; never fabricated, never dropped |
| `reader_lag_frames_max` | **nullable** — `null` when the undocumented `websockets` attribute chain breaks, or no source is bound | **correct pattern.** A fabricated 0 would read as "no backlog" |
| `reader_stall_ms_max` | cumulative **session watermark**, read from `TransportCounters` | **see §9.1 — this field is misnamed and mis-typed** |
| `rotation_failures`, `closer_outstanding_max` | gauges read from a bound archive source | **see §9.2** |
| `metric_flush_drops` | cumulative on purpose — a record reporting its own drop cannot exist | — |
| `subscription_generation` | a **gauge**: which stream this interval's numbers belong to | one observation per epoch, not per superseded router |
| `sequence_gaps` / `_regressions` / `_duplicates` | the closed fault vocabulary only | **does not include** the five fault classes of §8.3 |

**There is no `transport_dropped` field, and there must not be one.** The
installed `websockets` library has no drop path and no drop counter, so the
number has no source. A zero would be a fabricated measurement. Loss enters only
across a disconnect or upstream at the venue, and sequence integrity is the only
detector for the latter.

### 8.5 Latency

`LatencyEnvelope` is named at length on purpose:
`venue_to_receive_offset_contaminated_ms` equals
`true_transit + (our_offset − their_offset)`. **The host clock offset is not
characterised, so this is evidence, not a latency**, and the envelope says so on
its own artifact (`host_clock_offset_characterised: false`). Negative samples are
counted, never dropped — on the venue hop, negatives *are* the offset evidence.
`observation_gaps_measured: false`: every percentile is conditioned on "we were
connected", which biases the tail optimistically in exactly the regime that
matters.

Percentiles return `None` below `MIN_SAMPLES_FOR` (`p50` 3, `p95` 20, `p99` 100),
because *"p99 below 100 samples is just the maximum wearing a percentile's name"*.

A permanently-empty `normalize_to_book_us` hop was **deleted** rather than kept:
a permanently empty hop reads as "measured, and fast", which is worse than an
absent one.

**Production changed the sample size, not the epistemic status.** n = 84,154
samples: p50 **45.03 ms**, p90 47.93, p95 59.62, p99 511.61. It is reported
under the same contaminated name and **is still not a latency** —
`host_clock_offset_characterised: false` on the production artifact too. 84,154
samples of an uncharacterised offset is 84,154 samples of an uncharacterised
offset. Do not quote these as production latency.

---

## 9. CANDIDATES: benign numbers that should become typed `NOT_MEASURABLE`

Per the milestone's instruction, these are **identified and justified, not
implemented**. Each is judged against one test: *does the current API emit a
plausible number for something it cannot know?*

### 9.1 `reader_stall_ms_max` — **RECOMMEND CHANGE (two defects)**

`app/realtime/ws_transport.py:651-655`, `collector_metrics.py:1159-1166`.

**Defect A — the name is not the semantics (doctrine 8).** It measures the
interval between two *successful* `recv()` returns. On a quiet venue that is
**venue silence**, not reader stall. DEMO's measured rate is 0.75 frames/s on the
frozen pool and 0.00 in replication; a 90-second quiet stretch will be reported
as a 90 000 ms "reader stall". Nothing distinguishes *"our reader was blocked"*
from *"the venue said nothing"*. Recommended: rename to
`inter_frame_interval_ms_max`, or split into an explicit stall measure (time
between `recv()` *entry* and return with a non-empty inbound queue) and an
arrival-gap measure.

**Defect B — an unbound source reports 0, not unknown.** `stall_ms_max = 0` is
initialised and replaced only if a transport source is bound *and* returns a
valid dict. When the source is absent or its read fails, the record emits `0` —
indistinguishable from "no stall ever occurred". Its sibling
`reader_lag_frames_max` gets this right (`null` = UNAVAILABLE). Recommended: make
it nullable on exactly the same rule.

**Severity: does not block P4.** It is telemetry, not tape; it corrupts no book
and no venue fact. But it is a *plausible benign value emitted by a path that
cannot know*, on a field whose name asserts a cause it never measured — the exact
class doctrine 7 and 8 name.

**Defect A is now DEMONSTRATED ON PRODUCTION, not merely argued.** The session
reported `reader_stall_ms_max: 580` while the measured **maximum interarrival**
was **580.913 ms** — the same event, to the millisecond. The reader never
stalled: `frames_received == frames_yielded` (84,170 = 84,170), `read_timeouts:
0`, and `append_calls == events_received == records on disk`. The field measured
**venue quiet**, on a stream that never went silent for a whole second, and
named it a reader stall. The recommended rename to
`inter_frame_interval_ms_max` is upgraded from a reading of the code to a
reading of production data. *(P4 could not cross-check Defect B: the run emitted
no `kalshi-live-tape.jsonl` interval record at all, so `reader_lag_frames_max`,
`append_us_max` and `segment_close_ms_max` were **not captured** — absent, not
zero, and not to be reported as lag figures.)*

### 9.2 `rotation_failures` and `closer_outstanding_max` — **RECOMMEND CHANGE (nullable)**

`collector_metrics.py:1174-1179, 1212`. Both default to `0` when
`bind_archive_state`'s source is unbound or raises. The lane *is* wired today
(`collector.py:1646`), and a `read_source` failure is separately counted as
`source_failure` — so this is latent, not live. But the whole dict is built or
none of it is: one broken key takes both gauges to zero, and a `rotation_failures`
of 0 is exactly what the AGENTS.md doctrine table names as an already-shipped
defect in this repository (*"`closer_outstanding()` called on a property, so
`rotation_failures` silently fell back to 0 — indistinguishable from 'no rotation
failed'"*). It has been fixed once by accident of wiring; it is not fixed by
type. Recommended: `null` when the source is unavailable, on the
`reader_lag_frames_max` rule.

**Severity: does not block P4** — `CollectorResult.rotation_failures` is read
directly from the archive after `close()` and is the authoritative number; the
session even escalates to `STATUS_ARCHIVE_ERROR` on a non-zero.

### 9.3 `SubscriptionState.stats["recoveries"]` on replay — **RECOMMEND CHANGE (typed unknown)**

The strongest candidate by consequence. On replay this is **0 when the truth was
14** — not an absence, a *wrong number*, and one that would silently pass a
naive live-vs-replay equality check on the counter that measures how often the
collector had to repair itself. CP8 already surfaced this by splitting the
`checks.state_equality` bit.

Three options, in preference order:

1. **Move it out of `SubscriptionState.stats`** — the dict currently mixes a
   property of the *stream* with a record of an *operator action*. Cleanest, and
   it makes the tape-derived stats dict exactly equal to the live one.
2. **Emit a typed unknown from a tape-derived subscription** — a
   `recoveries: NOT_RECONSTRUCTABLE_FROM_TAPE` sentinel rather than 0.
3. **Archive the outbound command** (or a typed recovery marker) so the tape can
   answer *"did the collector resynchronise, and when"*.

Option 3 is the only one that makes the quantity *measurable*; options 1 and 2
make it *honest*. **Do 1 or 2 before a recovery count becomes an experimental
variable**; do 3 only if a preregistered experiment needs the timing.

**Severity: does not block P4.** No book, checksum, publishability flag or
ordering finding on any sequenced sid is affected.

### 9.4 `generation_advances` on an unsequenced sid — **RECOMMEND: DOCUMENT ONLY**

Live 2, tape-derived 0 on sid 2. Same shape as 9.3 but with a crucial difference:
**the per-record epoch stamps are all present and conserved**, so a reader can
always answer the question that matters ("which epoch did this ticker record
arrive under?"). Only the aggregate counter is lost, and it is trivially
recoverable by counting distinct `subscription_generation` values in the tape.
Typing it would add ceremony without adding knowledge. **Documented here; no
change recommended.**

### 9.5 `sequence_gaps = 0` on the `ticker` sid — **ALREADY TYPED. NO CHANGE.**

The CP8 checker carries `ordering_findings_establishable: false` beside it
precisely so the zero is never read as a clean measurement. This is the pattern
the other candidates should follow. **Confirmed correct; do not regress it.**

### 9.6 `events_rejected` under `dry_run` — **RECOMMEND: DOCUMENT ONLY**

In dry-run the archive is never constructed, so `events_rejected` is
*structurally* 0 and a frame the writer would have refused is indistinguishable
from one it would have accepted. The reported archived count is therefore an
**upper bound**. A dry-run session is a capacity probe, not evidence, and P4
archives for real — so this is a reporting caveat, not a contract defect.
**Documented; no change.**

### 9.7 `checks.state_equality` as a single bit — **ALREADY MITIGATED**

It folds the per-market comparison and the per-subscription counter comparison
into one bit, so it reads `false` while `state_equality.differences == []`. The
CP6–CP9 report splits it by hand. If that checker is reused for P4 it should
emit two bits. **Instrument-level, outside `app/`; noted for whoever runs P4.**

### 9.8 `OrderBook.stats["gaps"|"regressions"|"duplicates"]` — **STRUCTURALLY UNREACHABLE. FIXED AT THE READOUT (§10.3)**

Found by this milestone, and the sharpest instance of the class.

`SubscriptionRouter` settles ordering **once, per sid, before routing**, and then
calls `apply_delta(..., ordered_externally=True)`. Under that flag
`OrderBook.classify_seq` **never runs**. The book's own `gaps`, `regressions` and
`duplicates` counters are therefore dead code on the only path production uses —
they can never leave zero.

Those are exactly the numbers `replay()["stats"]` returns per market, and exactly
the numbers `kalshi-realtime-replay` printed:

```
    KXTEST-…      publishable=False  checksum=…
      snapshots=1 deltas=0 dups=0 gaps=0 regressions=0
```

on a tape carrying a **real, unrepaired four-message sequence gap**. Measured:

| | `replay()["stats"][market]` | `replay()["subscription_stats"]["1"]` |
|---|---:|---:|
| `gaps` | **0** | **1** |

A reader summing per-market `gaps` across a production tape gets **zero, always**.
This is the doctrine-7 shape in its purest form: not a metric that happened to be
zero, but a metric whose measurement path cannot become non-benign.

**This one is fixed rather than merely listed** (§10.3), because the misreading
happens on the operator's primary readout and the correct numbers were already in
the same return value, one key away.

### 9.9 REJECTED as candidates

- `spread_units = null`, `bid_levels = []` with a typed reason, `ladder_presence`,
  `reader_lag_frames_max`, absence of `transport_dropped`, `data_age_us` negatives
  — **all already correct.** Listed so a future reviewer does not "fix" them.
- `data_age_us` negative values are **kept**, not truncated at zero: truncating
  would bias the distribution optimistically and destroy the clock-offset
  evidence.

---

## 10. NARROWLY-REQUIRED CHANGES MADE BY THIS MILESTONE

Three, each argued against the contract. None is a semantics change: two are
operator-readout repairs and one deletes unreachable code. Nothing else in
`app/` is touched, and §10.4 records what was deliberately left alone.

### 10.1 `kalshi-realtime-replay` crashed on exactly the tape it exists to inspect — **FIXED**

`app/cli.py:828` formats `checksum={chk[:16]}`. `replay()` sets a market's
checksum to **`None` when the book is not publishable** — deliberately, so a
consumer comparing checksums cannot accept a torn book. So the default (text)
output path raises `TypeError: 'NoneType' object is not subscriptable` the moment
any market is halted or awaiting its generation snapshot.

Reproduced on venue-shaped records (one snapshot, one delta with a `seq` hole):

```
checksums:   {'M': None}
publishable: {'M': False}
TypeError: 'NoneType' object is not subscriptable
```

**Why this is in scope for a contract milestone.** After
`KALSHI-REPLAY-GENERATION-CONSISTENCY-001`, a non-publishable book is the
**normal** state for every market for a short window after every reconnect. The
one operator command for reading a production tape therefore dies on the fault it
exists to report, and dies *only* in the interesting case — the healthy tape
prints fine. That is a measurement-path defect of exactly the class this
milestone exists to close, and P4 is the milestone that would hit it first.

The fix prints the typed publication state instead of a truncated digest:

```
    <ticker>   publishable=False  state=book_halted  checksum=NOT_PUBLISHABLE
```

### 10.2 Dead unreachable code after `OrderBook.checksum`'s `return` — **REMOVED**

`app/realtime/book.py:880-885` is a second, different `payload`/digest block
after the `return`. It computes a checksum over `{market_ticker, yes, no}` with
no generation, no `last_seq` and no `sid` — i.e. the *pre-fix* digest, which
would call two books at different positions the same observation. Unreachable
today, and a trap for the next reader. Removed; no behaviour changes.

### 10.3 `kalshi-realtime-replay` printed a structurally-unreachable zero for the fault it exists to report — **FIXED AT THE READOUT**

§9.8's defect, at the surface where it does damage. The per-market line printed
`gaps` and `regressions` from `OrderBook.stats`, where they can never be
non-zero on the routed path, so a tape with a real sequence gap reported
`gaps=0` for every market.

**Only the readout is changed. No counter, no book, no replay semantics.** The
two unreachable fields are removed from the per-market line (which keeps the
counters that *are* reachable there, including
`rejected_pre_generation_snapshot`), and a per-**sid** block is printed with the
numbers that are actually measured — `accepted`, `gaps`, `regressions`,
`duplicates`, `wrong_sid`, `stale_generation`, `missing_seq`,
`generation_advances` — under a header that states where sequence integrity
lives. Two contract facts are printed beside them so they cannot be read off the
screen wrongly:

```
  sequence integrity is a property of the SUBSCRIPTION, not the market:
    sid=1    accepted=1 gaps=1 regressions=0 dups=0 …
    NOT MEASURABLE from a tape: `recoveries` is a COLLECTOR action and is not
      archived; a replayed value of 0 is not evidence that none happened.
    An UNSEQUENCED channel (ticker) has an EMPTY sequence domain: gaps=0 there
      is arithmetic, not an observation, and licenses no completeness claim.
```

**Why fix rather than list.** §9's candidates are *typing* decisions that change
an API contract, and the milestone reserves those. This is a display bug: the
correct numbers were already in the same `replay()` return value under
`subscription_stats`, and the command was printing the wrong key. Leaving it
would mean P4's operator reads `gaps=0` off a production tape and believes it.

### 10.4 What was deliberately NOT fixed, and why

> **2026-08-19: the deferral ended as intended.** B3 was patched by
> `KALSHI-P4-1-REPLAY-REQUAL` as a separate re-qualification — not during this
> milestone — and the CP8 numbers were re-derived rather than assumed. The
> reasoning below is preserved because it is the reasoning that produced the
> right sequencing.

**B3 (`replay()` does not consume an `error` frame's sequence number) is
reported, not patched.** The fix is roughly four lines — mirror
`SubscriptionRouter.dispatch`'s `needs_base=False` branch inside `replay()`'s
skip. It was not applied because `archive.replay()` is the lane CP8's
**QUALIFIED** verdict rests on, and changing its fault semantics means the
committed CP8 numbers no longer describe the shipped function. That is a
re-qualification decision, not a contract-milestone edit. The repository's own
standing bar says so: *"a live surprise is a FINDING, not something to patch
around during the qualification run."* It is pinned by a characterization test
(§13) so the fix turns it red and forces §11 B3 to be retired deliberately.

---

## 11. TRUE BLOCKERS FOR P4

Four as written; **B2 is struck on measurement**, leaving three. **B1 is operational and pre-existing** — P4 names it itself.
**B3 and B4 were found by this milestone** and are semantic: they are ways the
current code reaches a *wrong* answer, not a missing one.

| | blocks | closable by |
|---|---|---|
| B1 unverified production WS host + credential — **CLOSED 2026-08-20** | capture | an operator |
| ~~B2 unmerged branch stack~~ | ~~capture~~ | **STRUCK — already merged, verified on `main`** |
| ~~B3 `replay()` skips an `error` frame's `seq`~~ | ~~the **replay-equality verdict**~~ | **CLOSED 2026-08-19 — KALSHI-P4-1-REPLAY-REQUAL.** The verdict it blocked is now **COMPUTED and QUALIFIED** over the frozen production tape in two arms (KALSHI-P4-4) |
| B4 no session identity on the durable record — **run rule APPLIED 2026-08-20** | capture, **conditionally** | a run-procedure rule (no code) |

### B1. The production WebSocket host is UNVERIFIED — **CLOSED 2026-08-20 on measurement**

> **B1 CLOSES.** The production host has now been reached. HTTP **101** on
> `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, peer `16.58.202.54`,
> certificate read **off that same socket** between handshake completion and the
> collector's first `recv()`: SAN `["*.kalshi.com"]`, zero demo names,
> `TLS_AES_128_GCM_SHA256`. The credential was separately proven read-only on
> the venue's own testimony — `GET /trade-api/v2/api_keys` answered **200** by
> `api.elections.kalshi.com`, key `sha256:cfdd78afeded1c22` present, scopes
> `["read"]`, `proven_read_only: true`, `verified_before_first_frame: true`.
> Production and demo DNS sets are disjoint (8 addresses vs 2) and the
> certificates are cryptographically distinct (`*.kalshi.com` vs
> `CN=demo.kalshi.co`).
>
> The original objection was *"documentation and a certificate are both stronger
> than a name, and neither is a handshake."* There is now a handshake. The
> source comment at `app/realtime/kalshi.py:52-55` still says UNVERIFIED and
> **has deliberately not been edited** — that edit belongs with the B1 closure,
> not with a capture phase or a docs amendment.

The paragraph below is retained as the original statement of the blocker.


`app/realtime/kalshi.py:52-55`:

```python
# Corrected in KALSHI-DEMO-READONLY-VALIDATION-001 from `wss://demo-api.kalshi.co`.
# Neither host has been reached; both remain unverified until a demo connection
# is actually opened.
WS_HOSTS = {ENV_PRODUCTION: "wss://external-api-ws.kalshi.com" + WS_PATH, ...}
```

The demo host has since been reached repeatedly. **The production host has
never been reached.** The P4 milestone lists this as prerequisite 4 in its own
words. It also requires a production read-scoped credential whose scopes are
verified against the live key-metadata route — the DEMO credential is on EVO
only and is not a production credential.

### B2. ~~The branch stack is unmerged~~ — RESOLVED, verified 2026-08-17

**This blocker was STALE WHEN WRITTEN and is struck.** It was derived from P4's
prerequisite list — a document written *before* the merges — rather than from
the tree the analysis was standing in. Doctrine 9 applies to internal documents
too: a milestone's prerequisite list is a claim about the repository, and a
claim about reality must be checked against reality.

The branch this analysis started from (`fa52c9f`) **already contained** all four
fixes it names as missing. Verified by measurement on `main`:

| claimed missing | marker | occurrences in `main` |
|---|---|---|
| trade-sid basing defect | `needs_base` | **13** |
| absence/empty conflation | `ABSENT_NOT_SUPPLIED` / `no_ladder_supplied` | **19** |
| `max_seconds` defect | `wait_for` | **2** |
| CP7 per-market defect | `based_for_current_generation` | **3** |

Merge lineage: P0 at `1549984`, P2 at `52f4f31`, CP7 live re-run at `fa52c9f`,
this contract at `ccb95ec`. **P4's "same collector, unchanged" therefore holds
against `main` today.**

The remaining true blockers are **B1** (operational: production host and
credential), **B3** (blocks the replay-equality verdict only), and **B4**
(closable by one run rule, zero code).

B1 is not a P3 finding. They are stated because P4's entry criteria
name them and because a GO on the contract is not a GO on operational readiness.

### B3. `archive.replay()` manufactures a sequence gap on an `error` frame — **FOUND HERE. CLOSED 2026-08-19.**

> **OUTCOME — closed by `KALSHI-P4-1-REPLAY-REQUAL` (branch
> `KALSHI-P4-1-REPLAY-REQUAL`), 2026-08-19.**
>
> The §10.4 remedy was applied as written: `archive.replay()` now drives a
> sequenced non-orderbook record through the router instead of `continue`-ing
> past it, so a control frame consumes the number it occupies. The rule it
> enforces is **a sequenced frame must affect the relevant sequence domain even
> if that frame is not an order-book state mutation**. No venue semantics were
> added — `SubscriptionRouter.dispatch` already held this behaviour and replay
> simply never reached it.
>
> **Two narrowings, both deliberate and both guarded.** A control frame never
> *creates* a subscription, and a frame with no `seq` is still passed over.
> `replay()` remains **book replay**: §1's
> `test_a_non_orderbook_sid_never_becomes_a_subscription` and CP6-CP9's
> `test_the_shipped_replay_omits_the_non_orderbook_sids` are both still green,
> so this closure must not be read as widening replay to every sid. That remains
> a separate decision with its own guard.
>
> **The severity stated below was understated.** The table says live 0 / replay
> 1. Re-measured on a real digest-chained archive that actually contains the
> frame — built for this purpose, because no captured tape has one — it is
> **live 0 faults / replay 12**: the phantom gap halts the subscription, and
> every later delta is then refused with *"delta received while the subscription
> is not healthy"*. B3 cost the remainder of the tape, not one record. On that
> tape CP8's `state_equality` verdict itself **failed** before the fix and
> **passes** after it, while CP8's own `corrupt_one_delta` negative control
> still fails — the instrument reaches both answers.
>
> **The production tape did not and could not prove this.** The P4 tape
> (`p4-attempt2-20260820T003519Z`, 84,170 records, 7 segments) contains **zero**
> error frames, as do all three DEMO CP6-CP9 sessions. Replaying them cleanly
> after the fix proves only that nothing broke. B3's proof is separate and is
> the requalification described above. The production tape was not re-captured,
> mutated, or claimed.
>
> **The three characterization tests are RETIRED ON EVIDENCE** — the outcome
> §13 was written to produce. See §13 for the retirement and its net-stronger
> replacement.
>
> Evidence: `docs/experiments/KALSHI-P4-1-REPLAY-REQUAL/` —
> `before-fix-red.txt`, `after-fix-flip.txt`,
> `cp8-requal-PREFIX-fails.txt`, `cp8-requal-POSTFIX-passes.txt`.
> Guards: `tests/test_kalshi_p4_1_replay_requal_001.py` (22 tests).

**What follows is preserved in the present tense as the record of what was
found. It describes the defect, not the current code.**

**The one semantic defect this milestone found in a path P4 depends on.**

`replay()` skips every non-orderbook `event_type` with a bare `continue`
(`archive.py:1177-1179`) — *before* dispatch. The live lane does the opposite:
`SubscriptionRouter.dispatch` routes every frame carrying a `seq` through
`_accept(..., needs_base=False)` precisely because **control frames consume a
sequence number** (§3.3, wire-confirmed twice).

So replay never advances past the `seq` an `error` frame occupies, and reports a
gap where none exists. Demonstrated on the captured wire shape
(`{"type":"error","sid":1,"seq":3}` between deltas at seq 2 and seq 4):

| | live lane | `replay()` |
|---|---|---|
| faults | **0** | **1** |
| publication state | `publishable` | **`book_halted`** |
| fault text | — | `sequence gap: expected 3, got 4` |

**Why it is a blocker for the verdict and not for the capture.** It corrupts no
record. The tape is complete, digest-chained and re-replayable, so the fix can be
applied *after* a production capture and re-run over the same bytes. What it
cannot survive is P4 computing a replay-equality verdict with it in place — the
qualification would report loss the venue never caused, on a claim
(`Replay equality QUALIFIED`) that is one of the five things P4 exists to
re-establish in production.

**Why DEMO never caught it.** CP6–CP9 recorded **0 error frames** in all three
sessions, because the P0 fix removed the invalid command that was producing them.
The only capture that ever contained an orderbook-sid `error` frame is
2026-08-08, which predates `replay()`'s current shape. **A venue-initiated error
on the orderbook subscription in production triggers it immediately, and there is
no operational workaround** — we do not control which frames the venue sends.

**Remedy** (not applied here, §10.4): inside `replay()`'s skip branch, mirror the
live path — when a skipped record carries a `seq`, drive it through the router
with `needs_base=False` instead of `continue`. Recorded as `STILL DEBT` in
`KALSHI-REPLAY-GENERATION-CONSISTENCY-001`'s deferred list; this milestone
escalates it from debt to blocker because P4 is the milestone that will hit it.

### B4. The durable record carries no session identity — **FOUND HERE. CONDITIONAL.**

`subscription_generation` is monotonic **within one collection session** and
restarts at 1 in the next. This is documented as a known limitation of
KALSHI-TAPE-GENERATION. What the contract adds is the consequence:

**`RECORD_FIELDS` has no session field.** Not `session_id`, not anything —
`connection_generation` restarts too, and `segment_id` is
`venue.YYYY-MM-DDTHH[.rNNNN]`, i.e. wall-clock partition plus a rotation
counter, so a new session starting in a new hour is indistinguishable from a
rotation inside one. **The true sequence identity `(session, generation, sid)`
is not representable on the tape.**

Two sessions appended to one archive root therefore cannot be told apart by a
replay reader — and `read_verified()` returns *every* record for an environment
in committed order, which is what the CLI replays. Measured:

```
session 1 (generations 1, 2) then session 2 (generation 1 again):
  events_rejected      = every record of session 2
  stale_generation     = 2
  publishable          = {market: False}
  the identical session 1 alone replays clean, 0 rejected, publishable=True
```

Every record after the boundary is refused as a straggler from an epoch the
subscription has already left. That is the **correct** reading of the record
schema — which is why the schema is the defect.

**Remedy, and why this is conditional.** No code change is required if P4 adopts
one rule:

> **One archive root per collection session.** A P4 run that captures several
> sessions writes several archive roots and replays them separately. Concatenating
> them into one root produces a tape that cannot be replayed past the first
> boundary.

If P4 wants a single multi-session archive, the record envelope needs a session
identity — a `RECORD_SCHEMA_VERSION` bump, which is a schema decision outside
this contract's authority and must not be made silently.

**The run rule was APPLIED and it worked (2026-08-20).** P4 wrote one archive
root for one session (`~/kalshi-prod-tape/p4-attempt2-20260820T003519Z`, session
`s-20260820T003520Z-f450f75ed1fc`), and the tape verified `VALID` with
`records_read == records_expected == 84,170` and `head_state CURRENT`. **The
schema defect is not repaired — it is avoided**, exactly as this section
prescribed, and the rule must be carried into every subsequent production run.
One operational caveat found by P4 and recorded here because it bears on the
rule's mechanism: the session claim is published at `ROOT/production/` while the
archive writes to `ROOT/env=production/`, so the claim sits in an empty sibling
directory rather than "beside the genesis". **The guarantee still holds** —
`session_claim_path()` is deterministic and per-environment, so a second session
against the same root still collides and is refused — but the stated rationale
about *which* directory the boundary lives in is wrong and should be corrected
when someone owns that file.

---

## 12. KNOWN LIMITATIONS THAT DO **NOT** BLOCK P4

Each is either typed at the API, guarded by a test, or is a property of the venue
that no amount of work on this tape can change.

**Production status is recorded inline below, 2026-08-20.** No limitation is
retired by this amendment. Four are now **two-venue facts** (L1, L11, L17, L18);
three were **not exercised at all** in production and stay open on that basis
(L4, L5, L7, L8); three had their **missing empirical input supplied** (L9, L15,
L16); and L12's premise is now quantified against the venue it was written about.

| # | limitation | why it does not block P4 |
|---|---|---|
| L1 | **`ticker` completeness is permanently unknowable.** No `seq`, no gap detector, no repair. | Typed (`ordering_findings_establishable: false`), stated in this contract, and drift-detected. It is a *measurement-contract fact for* `MARKET-MICROSTRUCTURE-EDGE-001`, not a bug. **PRODUCTION: CONFIRMED, 0 of 2,395 records carry a `seq`.** Now a two-venue fact. `missing_seq = 0` on that sid is the same empty-domain artefact and is typed too. |
| L2 | **`recoveries` and `generation_advances` do not reconstruct.** | Collector actions, not market state. Every book, checksum, publishability flag and ordering finding on every sequenced sid reconstructs exactly (§8.2). Candidates in §9.3–9.4. |
| L3 | **`archive.replay()` is orderbook-only.** | Documented as the boundary (§1), pinned by a test, and no named downstream experiment requires more (§1.1). |
| L4 | **The `EMPTY` ladder state has never been observed live** — 0 in 360 DEMO snapshots **and 0 in 13 production snapshots**. Fixtures only. | The distinction it protects (`NOT_PROVIDED != EMPTY`) is exercised on real frames; only the third state is fixture-backed. **PRODUCTION: STILL NOT OBSERVED. L4 STAYS OPEN** — n=13 is far too small to retire it, and the distinction earned its keep anyway: 2 of 12 production markets ended `publishable` with zero levels on both sides, distinguishable from a genuinely empty book **only** by `ladder_presence` (§5.1, §7.1). |
| L5 | **The delta-refusal path (`rejected_pre_generation_snapshot`) has never been exercised live**, in either CP7 run **or in production**. | The guard exists and is unit-proven. The venue sent all 60 snapshots contiguously both times — luck, and reported as `NOT EXERCISED` rather than as a pass. **PRODUCTION: STILL NOT EXERCISED** — every snapshot again arrived before its deltas, and with one subscription generation there was no epoch boundary to refuse across at all. Three live runs of luck is not a proof. |
| L6 | **No natural sequence gap has ever been observed** on any DEMO subscription. Both gaps were forced. | The detector is proven not to fire falsely (0 in the control) and proven to fire on injected gaps (14/14, live and replay). Only the *origin* is ours. |
| L7 | **Whether a venue-initiated disconnect behaves like a forced one is not established.** | The reconnect ladder is bounded and the epoch model does not depend on the cause. **PRODUCTION: STILL NOT ESTABLISHED — 0 disconnects and 0 reconnects in 600 s.** The expectation that "P4 will run long enough to observe real ones" was wrong for a ten-minute run; a longer run is the only way to close this. |
| L8 | **Whether `error` consumes a `seq` on the orderbook sid** was not re-observed in P0 **and was not observed in production**. | The 2026-08-08 capture says yes and the code assumes yes — the conservative assumption. Assuming *no* would manufacture gaps; assuming *yes* cannot hide one. **PRODUCTION: NOT RE-OBSERVED — zero `error` frames in 600 s.** Recorded as not-observed, never as a pass. Note the coupling: the same absence is why B3 did not fire in production, so B3 is **unexercised, not disproven**. |
| L9 | ~~**Rotation constants rest on an assumed 500 events/s peak.**~~ **SUPERSEDED 2026-08-20 — the prior is now a measurement, and it was LOW.** Every CP session started **zero** rotations; production started **6** in ten minutes. | A rate question, and rate is exactly what P4 measured. **`~500 events/s` is no longer an assumption — it is a superseded design prior. The observed 1-second peak is 565 f/s, 13% ABOVE it: the sizing input was slightly LOW, not conservative.** 565 f/s is still ~12× under the ~6,900 f/s closer ceiling (L16), so nothing is unsafe — but the constant must stop being described as a conservative bound, and it is **universe-size-dependent**, not absolute. Full treatment, provenance and caveats in **§16.2**. |
| L10 | **`event_bytes_total` per-frame attribution is approximate** when a malformed frame precedes a good one. | Aggregate is exact; `frames_malformed` was 0 in every session measured. |
| L11 | **`venue_to_receive_offset_contaminated_ms` is not a latency.** Host clock offset uncharacterised. | Named at length, flagged on its own artifact, negatives retained. It is evidence and is labelled as such. |
| L12 | **DEMO is a load-test rig, not a market.** 98.3% of eligible frames come from 194 venue test instruments; the frozen 12-market pool produced 0.75 frames/s against a 100 000-frame floor (9.2× short) and **0.00/s** on replication. | **This is the entire reason P4 exists.** It is P4's premise, not an obstacle to it. **PRODUCTION: QUANTIFIED — ~187×.** Identically sized (12 markets), identically selected, same three channels, same collector: DEMO **0.75 f/s**, production **~140 f/s** mean, peak **565 f/s**, **0** silent seconds in 600, **6** rotations vs DEMO's **0** ever. **Every constant tuned against DEMO's rate was tuned against a regime ~187× slower.** |
| L13 | **`events_rejected` is structurally 0 under `dry_run`.** | P4 archives for real. §9.6. |
| L14 | **The wall-clock test flake class** (baseline 5,195 / 8, rotating membership). | Pre-existing, attributed by measurement, unrelated to any realtime module. |
| L15 | **The tape has no retention policy and no owner.** The collector milestone's own open question Q4 says so: *"a tape with an unowned growth curve is how the SQLite growth alert story started."* | Not a semantic defect. It is a P4 **operational** input: production rate is unknown until P4 measures it, so a retention rule cannot be sized before the run. It must be sized *from* P4's first hour, not guessed before it. **PRODUCTION SUPPLIED THE INPUT: ~13.8 MB compressed per 600 s at 12 markets = ~83 MB/hour, ~2.0 GB/day.** That is the number L15 was waiting for, for **this** universe size and **this** hour. **The tape still has no retention policy and no owner** — L15 does not retire; it now has a denominator. |
| L16 | **The closer's margin over the append ceiling is under 2×.** Synchronous append sustains ~3,440 events/s; the closer keeps up only below roughly 6,900 events/s at `DEFAULT_MAX_SEGMENT_RECORDS = 13_000`. | Overload is designed to become a **timestamped disconnect, not a silent gap** — append latency *is* reader stall, on purpose, and a collector that cannot keep up stops and says so. **PRODUCTION: MEASURED.** Peak **565 f/s** at 12 markets is **~12× under** the ~6,900 f/s closer ceiling and ~6× under the ~3,440 f/s synchronous-append ceiling. Append kept up for the whole session — `frames_received == frames_yielded == append_calls == records on disk == 84,170`, `read_timeouts 0`, `rotation_failures 0`. **Under naive linear scaling in market count the peak would reach the closer ceiling at roughly 145 markets** — an order-of-magnitude marker only: §16.3 measures per-market frame rates spanning four orders of magnitude, so frame rate is emphatically **not** linear in market count. |
| L17 | **Archive order equals wire order only under a single producer per subscription.** | Structural in the collector: exactly one task reads exactly one socket and calls `append()` on its own stack. Stated so a future concurrent writer does not silently void it. |
| L18 | **The SIGKILL loss window is the unflushed tail plus the whole uncommitted segment** (`flush_every = 256`; `close()` is the commit point and an unclosed segment is explicitly not evidence). | Bounded, documented, and the same for DEMO and production. Rename-after-fsync is the durability contract, not `close()`. |
| L19 | **`OrderBook.stats["gaps"|"regressions"|"duplicates"]` are structurally unreachable on the routed path** (§9.8). | Fixed at the operator readout (§10.3) and pinned by a test. The reachable numbers are in `subscription_stats`. A future consumer reading the per-market block must be told, and now is. |
| **L20** | **Every production rate in this contract comes from ONE ten-minute overnight window.** 00:36–00:46 UTC (20:36 ET), a sports-dominated 12-market universe (MLB, ATP, MLS, WNBA in play), one session, one venue hour. **ADDED BY THIS AMENDMENT.** | It does not block anything — it **bounds every number in §16**. A single window cannot estimate a peak: 565 f/s is *the largest second we saw*, not *the largest second there is*, and the true daily peak is unbounded above by this evidence. Rates at other hours, on other weekdays, and for non-sports series are **unmeasured**. Any capacity, retention or cost claim that quotes §16 must quote this limit with it. |
| **L21** | **Trading rate is a WEAK proxy for message rate — Spearman ≈ 0.52 on our own production data.** `top_of_book_change_rate` is degenerate as a screen (1.00 for 11 of 12 markets, Spearman **−0.20**). **ADDED BY THIS AMENDMENT.** | It does not block P4 — the capture is done and the tape is valid. It **changes research design**, which is why it is stated as a rule in **§16.3** rather than a caveat here: a universe stratified on REST trading rate is **not** stratified on message rate, and selecting a microstructure universe that way is the subtler recurrence of the manifest mistake (doctrine 8). |

---

## 13. TESTING BAR — positive controls

Per doctrine 7 and the milestone's bar: *a healthy zero is not sufficient unless
the measurement path has been demonstrated capable of becoming non-zero.*

**Already proven capable of becoming non-benign** (retained, not re-written):

| force this | this becomes non-benign | evidence |
|---|---|---|
| a reconnect | `subscription_generation` advances, epochs `{1,2,3}` on the tape | CP7 live re-run |
| a sequence gap | `sequence_gaps` 0→1; replay faults 0→14 | `s3-drop` |
| a per-market boundary | 59 separate publishability acquisitions, not one | CP7 re-run |
| a corrupted `normalized` | conservation check FAILS | CP8 negative control |
| a corrupted delta `seq` | state equality FAILS | CP8 negative control |
| reverting the generation fix | 17 of 25 tests turn red | `test_kalshi_replay_generation_consistency_001.py` |
| ticker gaining a `seq` | the drift detector FAILS | `test_the_ticker_channel_still_carries_no_sequence_number` |
| pointing the CP7 verifier at the FAILED artifact | it fails, and fails on the CP7 shape | *"a checker that cannot fail is not a check"* |

**Added by this milestone** — `tests/test_kalshi_tape_measurement_contract_001.py`,
**14 tests, all green**, narrow and only where a contract claim had no guard.
Every one carries its own anti-vacuity control.

| test | claim it guards | its anti-vacuity control |
|---|---|---|
| `test_a_non_orderbook_sid_never_becomes_a_subscription` | §1: `replay()` is book replay — three sids on the wire, one in the output | the orderbook sid *was* processed (2 applied, publishable) |
| `test_the_tape_itself_is_not_the_limitation` | §1: the same records re-derive the trade sid's ordering | that sid's gap detector fires on an injected hole |
| ~~`test_live_absorbs_the_error_frames_sequence_number`~~ | ~~§11 B3, live half~~ | RETIRED 2026-08-19 — see below |
| ~~`test_replay_manufactures_a_gap_that_never_happened`~~ | ~~§11 B3, replay half~~ | RETIRED 2026-08-19 — see below |
| ~~`test_anti_vacuity_without_the_error_frame_the_lanes_agree`~~ | ~~§11 B3~~ | RETIRED 2026-08-19 — see below |
| `test_the_durable_record_has_no_session_identity` | §11 B4 | the fields the contract *does* claim are pinned are present |
| `test_replaying_two_sessions_halts_every_book_at_the_boundary` | §11 B4 | the identical first session alone replays clean |
| `test_omitted_and_empty_ladders_are_distinguishable_after_replay` | §5.1, §7: `NOT_PROVIDED != EMPTY` survives replay | `checksum()` **cannot** tell them apart — which is why the check exists |
| `test_a_market_awaiting_its_own_snapshot_is_not_halted` | §7.1, §8.3: a reconnect boundary is not an integrity fault | a real within-generation gap **does** halt |
| `test_the_three_candidates_emit_zero_while_the_correct_one_emits_null` | §9.1, §9.2 | the record really was built and is schema-valid |
| `test_per_market_fault_counters_are_structurally_unreachable` | §9.8 | the reachable per-market counters did move |
| `test_there_is_no_transport_dropped_field` | §8.4 | the schema is populated and the one legitimate drop field is there |
| `test_text_output_reports_the_halt_instead_of_crashing` | §10.1, §10.3 | — |
| `test_anti_vacuity_a_healthy_tape_still_prints_a_real_checksum` | §10.1 | a healthy tape prints a real digest and `gaps=0` truthfully |

Three are **characterization** tests (`B3`×2, `B4`×1): they pin behaviour this
document reports as a defect, not behaviour it endorses. That is the
repository's own pattern — pinning a limitation is what makes it *retire on
evidence*. **When B3 or B4 is fixed its test turns red, and whoever fixes it must
delete the corresponding paragraph from this contract.**

#### The B3 characterization tests are RETIRED ON EVIDENCE — 2026-08-19

**The mechanism worked exactly as designed, and this is the record of it.** When
`KALSHI-P4-1-REPLAY-REQUAL` applied the §10.4 remedy,
`test_replay_manufactures_a_gap_that_never_happened` turned red on the same
commit — `1 failed, 29 passed`, captured in
`docs/experiments/KALSHI-P4-1-REPLAY-REQUAL/after-fix-flip.txt`. The three tests
of `TestErrorFrameSequenceDivergence` are retired, and the class is replaced by
`TestErrorFrameSequenceIsConsumedByBothLanes`.

**The replacement is a flagged amendment to a standing audit and is
net-stronger**, per doctrine:

- it asserts the two lanes now **agree** on the input that used to split them —
  a claim about *both* lanes, where the retired pair each described one;
- it **retains** the original anti-vacuity control (the divergence was caused by
  the error frame and by nothing else);
- it **adds** a second one: the same pair must still report a fault when a drop
  actually happened, because *"both lanes are quiet"* is otherwise satisfiable
  by two lanes that have both gone blind — the exact failure mode a sequence fix
  risks introducing.

The full closure argument — wire provenance under doctrine 9, the
ladder-non-mutation proof, determinism, the scope boundary, and the CP8
requalification — lives in `tests/test_kalshi_p4_1_replay_requal_001.py`
(22 tests). **§11 B4's characterization test is still live and still pinning an
open defect**; B3's closure says nothing about it.

---

## 14. WHAT THIS MILESTONE ADDED

1. This document — the canonical P3 measurement-contract artifact.
2. `tests/test_kalshi_tape_measurement_contract_001.py` — 14 narrow guards.
3. Three narrowly-required fixes (§10.1, §10.2, §10.3), each argued individually
   and none of them a semantics change.

**Deliberately NOT done, and each said out loud rather than omitted:**

- **Tape replay** (§1.1) — no named downstream experiment requires it today.
- **The `NOT_MEASURABLE` retypings** (§9) — candidates with reasoning; the
  implementation is a separate decision, as the milestone requires.
- **The B3 fix** (§10.4) — a re-qualification decision, not a contract edit.
- **The B4 schema bump** (§11 B4) — a `RECORD_SCHEMA_VERSION` change, and an
  operational rule closes it without one.
- **Any production observation whatsoever.** P3 and P4 are not rolled together.

---

## 15. SAFETY

Read-only. **No venue was touched by this milestone.** Every venue measurement
quoted here is from artifacts already committed to the repository. The only code
executed was pure and offline: `replay()` and `SubscriptionRouter` over
synthetic, venue-shaped records, and the new test file against `tmp_path`
archives. **No socket was opened, no credential was read, copied or printed, no
database session was created.** EVO was not contacted; the live Solana lane was
not disturbed.

Safety grep over `app/realtime/`: **two hits, both boundary-statement docstrings**
(`collector.py:862`'s `BOUNDARY_NOTE` and `auth.py:8`'s OBSERVE_ONLY statement).
No implementation surface. Repo-wide count unchanged from baseline.

`BOUNDARY_NOTE`, carried on every `CollectorResult` including refusals:

> OBSERVE_ONLY: authenticated read-only Kalshi market-data observation. The
> session sends channel subscriptions only, over a closed allowlist; no order,
> position, wallet, key-management or write-scoped surface exists in this
> package.

`FORBIDDEN_CHANNELS` (`fill`, `market_positions`, `user_orders`,
`communications`, `order_group_updates`) is checked at `CollectorConfig`
construction — a forbidden channel in a config file, an environment variable or a
CLI argument is a `CapabilityError` before any object exists that could open a
socket.

**This amendment (§0.1, §16, §17) is docs-only and did not change that.** No
socket was opened, no credential read, no file under `app/`, `tests/` or
`scripts/` modified. Every production number quoted is from artifacts already
committed to the repository, or from an independent recount over them (§16.2.1).

---

## 16. PRODUCTION-MEASURED QUANTITIES — **ADDED BY AMENDMENT, 2026-08-20**

Everything in §1–§15 above was derived from DEMO. This section holds the
quantities that **only production could supply**, under the same discipline: a
provenance, a reconstructability class, and a stated limitation on every number.

**Provenance of the whole section.** Session `s-20260820T003520Z-f450f75ed1fc`,
production environment, `wss://external-api-ws.kalshi.com/trade-api/ws/v2`,
00:36:00.199Z → 00:46:01.437Z, `status=capped_time` (`max_seconds=600`).
**84,170 records, 7 segments (7 closed, 0 open, 0 invalid), 599.643 s span, 12
markets, 3 channels.** Archive verifier: `VALID`, `reasons []`, `warnings []`,
`records_read == records_expected == 84,170`, `truncated_records 0`,
`head_state CURRENT`. `git diff main -- app/` empty — the collector ran exactly
as shipped. Evidence: `docs/milestones/KALSHI-PROD-QUAL-CAPTURE-2-FINDINGS.md`,
`docs/evidence/KALSHI-PROD-QUAL-CAPTURE-2-*.json`.

### 16.1 `wire_frame_rate` — a new contract quantity

The contract had no entry for *how fast frames arrive*, because DEMO could not
produce a meaningful one. It has one now, and it needs typing more than most: it
is the quantity most likely to be quoted outside the window that produced it.

| attribute | value |
|---|---|
| **provenance** | **`COLLECTOR_FACT`.** The venue never states a rate. This is *frames that crossed our socket*, timestamped by our clock (`received_at_utc`, `received_monotonic_ns`). It is an observation about a joint system — venue, network, host — not a venue fact, and must never be written as one. |
| **wire representation** | none. No frame carries a rate. |
| **normalized representation** | none. Not a field on any record; computed by the reader from record timestamps. |
| **channel and SID semantics** | measured **across all sids**, decomposable per sid (mix below). Per-sid rates are the useful ones; the aggregate is what the archive layer sees. |
| **sequence domain** | not applicable — a rate is not sequenced. Note the asymmetry: on sids 1 and 3 the *count* is verifiable against `seq` contiguity; on sid 2 it is not (L1). |
| **ordering guarantees** | inherits §3 per sid. The rate itself asserts no ordering. |
| **frame-loss detectable?** | **only on the sequenced sids.** A measured rate is a **lower bound** on the venue's emission rate wherever loss cannot be excluded — which on `ticker` is always. |
| **source completeness knowable?** | **NO, and it matters here.** Every rate is conditioned on *"we were connected"*. This session had 0 disconnects, so the condition is trivially satisfied **for this window and no other** — the same optimistic conditioning §8.5 names for latency. |
| **subscription-generation semantics** | one generation for the whole window; a rate spanning a reconnect must be reported per generation or not at all. |
| **units / precision** | frames per second. Two means are reportable and differ by denominator — see below. Peaks are integer counts in 1-second buckets, and depend on bucket alignment (§16.2.1). |
| **missing-value semantics** | a bucket with no frames is **`0`, and that zero is an observation** — the one place in this contract where a zero is legitimate, because the collector was connected and counting. `silent_seconds = 0` here. It is **not** the same as an unmeasured second: a window we were disconnected for is `NOT_MEASURABLE:not_connected`, never 0. |
| **reconstructability** | **`DERIVABLE_FROM_RAW`** in the tape's sense — `received_at_utc` is on every record, so any reader can recount every figure below from the archive. It is *not* `RAW_REPLAYABLE`: the timestamps are collector facts, so a different host would produce different numbers from the same venue behaviour. |
| **current replay support** | **not `archive.replay()`.** `replay()` is book replay (§1) and returns no rate. Recounting is a plain read of `read_verified()`. |
| **positive control** | the measurement path is demonstrably capable of a non-benign value: DEMO's identically-computed rate read **0.75 f/s**, and its replication read **0.00 f/s**, on the same code. A rate reader that could only print a healthy number would not have printed those. |
| **known limitations** | **L20** (one ten-minute overnight window), **L12** (DEMO is not comparable), **L21** (the universe was not selected on message rate). All three apply to every row below. |

**Measured (12 markets, 3 channels, 599.643 s):**

| | measured |
|---|---|
| frames | **84,170** |
| mean, by observed span (÷ 599.643 s) | **140.37 f/s** |
| mean, by calendar-second buckets (÷ 601 buckets) | **140.05 f/s** |
| median, 1-second bucket | **115 f/s** |
| **peak, 1-second bucket** | **565 f/s** — next two peak seconds **523** and **455** |
| silent seconds | **0 of 600** |
| burstiness (index of dispersion) | **54.55** (Poisson = 1.0) |
| interarrival | p50 **1.135 ms** · p90 17.31 · p95 31.18 · p99 92.14 · max **580.91 ms** |
| bytes | 23,395,915 received · **~13.8 MB compressed on disk** |
| rotations | **6**, at exactly 13,000 records each plus a 6,170 partial — one per **~92.6 s** |

**The two means are both correct and are not interchangeable.** 140.37 divides
by the observed span between first and last frame; 140.05 divides by the 601
calendar seconds the session touched, whose first and last are partial. Quote
the denominator with the number. Nothing turns on the 0.3 f/s difference; the
habit does.

**Channel mix — the book feed IS the load.** `orderbook_delta` 79,243 (94.1%) ·
`trade` 2,516 (3.0%) · `ticker` 2,395 (2.8%) · `orderbook_snapshot` 13 ·
`subscribed` 3. A sizing exercise that ignores `ticker` and `trade` entirely is
wrong by under 6% at this universe; one that ignores the book feed is wrong by
16×.

**Continuous but violently bursty.** Not one silent second in ten minutes, and a
dispersion index of 54.5 with a p50 interarrival of 1.1 ms against a p99 of
92 ms. **Sizing on the mean understates the observed peak by ~4×.**

### 16.2 `~500 events/s` is a SUPERSEDED DESIGN PRIOR, and it was LOW

`segment.py:200-208` chose `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` to target a
~2 s close, and sized the rotation cadence against a **"~500 events/s assumed
peak."** That sentence is the only empirical claim the constant rests on.

> **It is no longer an assumption. The comparable measured quantity — the
> observed 1-second peak — is 565 f/s. The prior is EXCEEDED by 13%.**

**Record it as a LOW input, not a conservative one.** That distinction is the
whole point: a conservative input is one reality stays under, and reality did not
stay under this one. The constant is nonetheless **safe**, for a reason that has
nothing to do with the prior's accuracy — 565 f/s sits **~12× under the
~6,900 f/s closer ceiling** (L16) and ~6× under the ~3,440 f/s synchronous-append
ceiling, and the session ran with `rotation_failures: 0` and perfect append
conservation. **Safe by margin, not by forecast.**

**Which bound is load-bearing has inverted.** In DEMO the record bound was
unreachable and `DEFAULT_MAX_SEGMENT_AGE_S = 900 s` was the only thing that ever
rotated. In production the record bound fired every ~93 s and **the age bound
never fired at all**. `DEFAULT_MAX_SEGMENT_BYTES = 32 MiB` was ~15× away
(~2.1 MB/segment) and is not load-bearing either.

**Verdict: `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` does not need retuning for a
12-market universe — but it must stop being described as a conservative bound,
and it must be understood as universe-size-dependent.** Retuning is a rate
question, and rate is universe-dependent (§16.3): a different universe moves the
peak, and the peak is what this constant is sized against.

#### 16.2.1 A CORRECTION to the P4 findings document — 485 vs 565

`docs/milestones/KALSHI-PROD-QUAL-CAPTURE-2-FINDINGS.md` §5.1/§5.2 and
`docs/evidence/KALSHI-PROD-QUAL-CAPTURE-2-capture.json`
(`load.frames_per_second_peak_1s`) report the 1-second peak as **485 f/s**, and
§5.2 concludes from it that the ~500 e/s prior was *"~3% high — essentially
correct."*

**An independent recount over all 84,170 records, bucketed by calendar second,
gives 565 f/s** — next two peak seconds **523** and **455**, mean **140.05**
over 601 buckets. **This contract adopts 565.**

The two figures differ because they bucket on different boundaries: a 1-second
window's contents depend entirely on where its edges fall. 485 is therefore not
*wrong*, it is *a different alignment* — and **a peak is an upper-bound
statistic, so the larger valid alignment is the one a capacity claim must use.**
Taking the smaller one biases the estimate downward exactly where downward bias
is most dangerous.

**The conclusion reverses, not merely the number.** At 485 the prior reads 3%
high and vindicated; at 565 it reads **13% low and exceeded**. Recorded as a
correction rather than a silent overwrite because the findings document's
*reasoning* is sound and its warning still stands: *comparing an assumed **peak**
against an observed **mean** and calling the assumption "3.5× conservative" is a
category error.* That warning is right. It is the peak that was mismeasured, not
the argument.

**Downstream figures that move with it:**

| | at 485 (findings) | **at 565 (adopted)** |
|---|---|---|
| vs the `~500 e/s` prior | 3% high, "essentially correct" | **13% LOW, exceeded** |
| peak ÷ mean | 3.5× | **~4.0×** |
| headroom vs the ~6,900 f/s closer ceiling (L16) | ~14× | **~12×** |
| naive linear market-count marker | ~170 markets | **~145 markets** |

The market-count marker is an order-of-magnitude sanity check and nothing more —
§16.3 shows per-market frame rates spanning four orders of magnitude, so frame
rate is **not** linear in market count and 145 is not a capacity limit.

**Not applied to the source artifacts, deliberately.** The evidence JSON is a
frozen record of what the capture tooling computed and must not be rewritten
after the fact; the findings document belongs to P4. Both should carry a pointer
to this subsection. **If a third recount disagrees with 565, that is a finding
about the counting method and it supersedes this paragraph, not §16.1's table
silently.**

### 16.3 THE UNIVERSE-SELECTION FINDING — this changes research design

The P4 universe was selected by stratifying markets on **trading rate** (traded
contracts/minute) from a REST census. The tape then measured what those markets
actually did on the wire.

| manifest rank | wire rank | stratum | traded c/min | frames in 600 s |
|---:|---:|---|---:|---:|
| 1 | 1 | high | 29,328.1 | 19,741 |
| 2 | 5 | high | 23,582.8 | 9,744 |
| 3 | **8** | high | 19,534.9 | 1,914 |
| 4 | 2 | high | 18,865.5 | 17,655 |
| 5 | 3 | medium | 202.2 | 12,510 |
| 6 | 7 | medium | 197.0 | 2,978 |
| 7 | **12** | medium | 197.0 | **1** |
| 8 | 6 | medium | 196.8 | 6,962 |
| 9 | 9 | low | 16.2 | 715 |
| 10 | 11 | low | 15.8 | 28 |
| 11 | 10 | low | 15.8 | 34 |
| 12 | **4** | **low** | **15.7** | **11,885** |

```
Spearman(trading rate, wire frame rate) ≈ 0.52          n = 12
```

Directionally right in aggregate — high 58.3% of frames, medium 26.7%, low
15.0% — and **severely wrong per market**:

- the market ranked **last of twelve on trading rate produced the 4th-most wire
  frames** (11,885 in ten minutes);
- a **medium**-stratum market produced **exactly one frame in 600 seconds**;
- the selection statistic spans **1,868×** across the universe while wire frames
  span **19,741×**. The screen is not measuring the thing that varies.

**`top_of_book_change_rate` is degenerate as a screen.** It read **1.00 for 11 of
12** markets and its Spearman against wire frames is **−0.20**. At the probe
resolution used (4 reads over 7.6 minutes) it reduces to *"did top-of-book move
at least once"*, which is almost always yes. A statistic that is 1.00 for 92% of
its inputs is not ranking anything — doctrine 8, applied to one of our own
statistics rather than to a venue's field.

**The operational rule, and it is a rule:**

> **Do not select the microstructure universe using trading volume/activity as a
> proxy for message activity. Our own production measurement says that
> relationship is too weak.**

**This is the subtler recurrence of the manifest mistake.** That one was
`updated_time` → presumed freshness, and it reported 73,057 of 73,630 markets
stale. This one is **`trading volume` → presumed microstructure activity**, and
the two are now *measured* as not interchangeable. The first was caught because
its output was absurd. This one produces a universe that looks entirely
reasonable and is silently mis-stratified — which makes it the more dangerous of
the two.

**Consequences, stated so they are not rediscovered:**

1. **A universe stratified on REST trading rate is NOT stratified on message
   rate.** Any `MARKET-MICROSTRUCTURE-EDGE-001` design that wants message-rate
   strata must **measure message rate**, which requires a **WebSocket probe** and
   cannot be done from a REST census.
2. **Every rate in §16.1 inherits this.** 140 f/s mean and 565 f/s peak are
   properties of *this* twelve markets, chosen by a screen now known to be weakly
   related to the quantity being measured. They are not "the rate of a 12-market
   universe"; they are the rate of *these* twelve.
3. **Capacity extrapolation by market count is doubly unsound** — once because
   frame rate is not linear in count, and once because *which* markets are
   counted matters more than *how many*, by four orders of magnitude.
4. The manifest already declares this limitation on its own face
   (`STATISTIC_LIMITATIONS`, `stronger_alternative_not_used`). It is now
   **quantified**, which is the difference between a caveat and a finding.

### 16.4 What production did NOT settle

Recorded here as well as in §12, so that a reader of this section alone cannot
mistake a ten-minute clean run for a qualification of everything:

| unobserved | production evidence | limitation |
|---|---|---|
| the `EMPTY` ladder state | 0 of 13 snapshots | **L4 open** |
| `error`-frame behaviour on production, hence whether it consumes a `seq` | **0 error frames in 600 s** | **L8 open** |
| venue-initiated disconnect | **0 disconnects, 0 reconnects** | **L7 open** |
| the delta-refusal path `rejected_pre_generation_snapshot` | never exercised; every snapshot preceded its deltas | **L5 open** |
| the per-market generation-boundary path | one `subscription_generation` throughout | evidence stays CP7/DEMO |
| scaling past 12 markets | measured at 12 only — and §16.3 says count is the wrong axis | **L20** |
| **any hour but this one** | one window, 20:36 ET, sports-dominated universe | **L20** |
| host clock offset, hence any true latency | uncharacterised; 84,154 contaminated samples | **L11** |
| interval telemetry (`reader_lag_frames_max`, `append_us_max`, `segment_close_ms_max`) | **no `kalshi-live-tape.jsonl` was emitted** — absent, not zero | §9.1 |

**Replay equality is not in this table and is not this amendment's to report.**
See §11 B3, which its owner is closing.

---

## 17. AMENDMENT CHANGELOG — 2026-08-20

### What production CONFIRMED (DEMO provenance retained, never overwritten)

| # | property | where |
|---|---|---|
| 1 | `orderbook` **independently sequenced** — 79,256 records, contiguous 1 → 79,256, all fault counters 0 | §3, §3.2 |
| 2 | `trade` **independently sequenced** — 2,516 records, contiguous 1 → 2,516 | §3, §3.2 |
| 3 | `ticker` **UNSEQUENCED** — 0 of 2,395 carry a `seq`; `missing_seq = 0` is the same empty-domain artefact | §3, §3.2, L1 |
| 4 | sid assignment follows ack order; acks carry **no top-level `sid`**; the orderbook sid is discovered from **frames**, not from the subscribe | §3.1 |
| 5 | **snapshot ladder typing reproduced** — 10 `PRESENT/PRESENT`, 1 `NOT_PROVIDED/PRESENT`, 2 `NOT_PROVIDED/NOT_PROVIDED` | §5.1 |
| 6 | the `use_yes_price` / no-complement convention — **0 locked-or-crossed** in 2,405 spread samples | §5.1 |
| 7 | `ladder_presence` is load-bearing on real data — 2 of 12 markets ended `publishable` with zero levels on both sides | §5.1, §7.1 |
| 8 | ticker field naming settled — `yes_bid_dollars`/`yes_ask_dollars` on **2,395 / 2,395** | §5.4 |
| 9 | envelope, digest chain, rotation and commit semantics — archive `VALID`, 84,170 = 84,170, 7/7 segments closed, `head_state CURRENT` | §4.2 |
| 10 | the B4 **run rule works**: one archive root, one session, no schema bump | §11 B4 |

### What production CHANGED

| # | change | where |
|---|---|---|
| 1 | **`~500 events/s` is no longer an assumption — it is a SUPERSEDED DESIGN PRIOR, and it was 13% LOW.** Observed 1-second peak **565 f/s**. Safe by a ~12× margin against the closer ceiling, not by the prior's accuracy | §16.2, L9, L16 |
| 2 | **Production is ~187× DEMO** on an identically sized and selected 12-market universe — ~140 f/s vs 0.75 f/s, 0 silent seconds vs mostly silence, 6 rotations vs 0 ever | §16.1, L12 |
| 3 | **The load-bearing rotation bound inverted** — records fire every ~93 s; the 900 s age bound never fired; the bytes bound is ~15× away | §16.2 |
| 4 | **The universe-selection rule** — Spearman(trading rate, wire frame rate) ≈ **0.52**; `top_of_book_change_rate` degenerate (1.00 for 11 of 12, Spearman **−0.20**). *Do not select a microstructure universe on trading volume* | §16.3, L21 |
| 5 | **L15 has its denominator** — ~83 MB/hour, ~2.0 GB/day compressed at 12 markets. Still unowned | L15 |
| 6 | **B1 CLOSED on measurement** — HTTP 101 on the production host, certificate read off the capture socket, credential scopes `["read"]` attested by the venue | §0, §11 B1 |
| 7 | **§9.1 Defect A demonstrated on production data** — `reader_stall_ms_max: 580` equals the maximum *interarrival* (580.913 ms) in a session where the reader never stalled | §9.1 |
| 8 | **A correction to the P4 findings document** — the 1-second peak is **565, not 485**, and the conclusion reverses from "prior 3% high, essentially correct" to "prior 13% low, exceeded" | §16.2.1 |
| 9 | Two limitations added: **L20** (every rate comes from one ten-minute overnight window) and **L21** (trading rate is a weak proxy for message rate) | §12 |

### What remains UNOBSERVED after production — nothing here is softened

- the **`EMPTY`** ladder state — 0 of 13 production snapshots on top of 0 of 360 DEMO snapshots (**L4**);
- **`error`-frame behaviour on production** — zero arrived in 600 s, so whether an `error` consumes a `seq` on the orderbook sid is still unsettled and the conservative assumption stands (**L8**);
- **venue-initiated disconnect** — zero disconnects, zero reconnects (**L7**);
- the **delta-refusal path** `rejected_pre_generation_snapshot` — never exercised; three live runs of luck is not a proof (**L5**);
- **scaling past 12 markets** — and §16.3 argues market count is the wrong axis anyway;
- **any hour but this one** — 00:36–00:46 UTC, 20:36 ET, sports-dominated universe, a single window (**L20**);
- **host clock offset**, hence any true latency (**L11**);
- **interval telemetry** — no `kalshi-live-tape.jsonl` was emitted at all: absent, not zero (**§9.1**).

Every `NOT_MEASURABLE` and `NOT_RECONSTRUCTABLE` state in §7–§9 and §12 is
carried forward **unchanged**. Production emitted them as typed states rather
than as zeroes — `ticker_sequence_gaps: NOT_MEASURABLE:empty_sequence_domain`,
`ticker_completeness: NOT_MEASURABLE:no_loss_detector_exists`,
`transport_dropped_frames: NOT_MEASURABLE:no_source_exists`,
`recoveries_from_tape: NOT_RECONSTRUCTABLE_BY_DESIGN`,
`generation_advances_on_unsequenced_sid: NOT_RECONSTRUCTABLE_BY_DESIGN` — which
is this contract working, on production, for the first time.

### Nothing production CONTRADICTED

**No production observation contradicted a semantic claim in §1–§15.** Every
DEMO-derived property that production exercised, production reproduced. The one
reversal in this amendment (§16.2.1) is a correction of a *P4 figure* against a
recount of the same tape — an arithmetic disagreement between two readings of
one artifact, not production disagreeing with the contract. Two contract
statements were *superseded by measurement* rather than contradicted: L9's
assumed peak (now measured, and low) and L12's DEMO baseline (now quantified).

### Deliberately NOT done by this amendment

- **§11 B3 untouched.** Its owner is closing it; the replay-equality verdict is not this amendment's to report.
- **No `app/`, `tests/` or `scripts/` edit.** `app/realtime/kalshi.py:52-55` still says the production host is unverified even though §11 B1 now closes; that edit belongs with the B1 closure, not with a docs amendment.
- **No edit to `KALSHI-PROD-QUAL-CAPTURE-2-FINDINGS.md` or to any evidence JSON.** The 485 → 565 correction is recorded in §16.2.1 and pointed at from here; an evidence artifact is a frozen record of what the tooling computed and must not be rewritten after the fact.
- **No limitation retired.** Four became two-venue facts (L1, L11, L17, L18); none was closed.
- **No new test.** The contract's own testing bar (§13) is unchanged; §16's quantities are measurements from a committed artifact, not new claims about `app/` behaviour that a guard could pin. A recount harness for §16.2.1 would be the obvious next guard and is **not** written here.
- **The four secondary P4 readout findings** (`subscription_generations = 3`, `segments_committed = 1`, `healthy = False` on the unsequenced sids, the session-claim directory) are noted only where they bear on this contract; they belong to their own owner.

### Editorial note

§2 describes "the remaining eight" per-field attributes and then enumerates nine
(provenance, wire representation, normalized representation, units/precision,
missing-value semantics, reconstructability, current replay support, positive
control, known limitations). §16.1 reproduces the attribute set **faithfully**
rather than dropping one to make the count come out. The discrepancy is
editorial, predates this amendment, and is flagged rather than silently patched.


---

## §16.2.2 — the peak is 612 f/s, and the capture tooling UNDERSTATES it

Third and final correction to this number. Measured on the same 84,170 records,
which carry **sub-second receive timestamps** (verified), so a sliding window is
computable and is the correct statistic:

| method | peak 1 s | vs the superseded ~500 f/s prior |
|---|---:|---|
| capture tooling (`…-capture.json`, `frames_per_second_peak_1s`) | **485** | 3% below |
| calendar-second recount (§16.2.1) | **565** | 13% above |
| **sliding 1 s window — ADOPTED** | **612** | **22% above** |

Peak at `2026-08-20T00:45:22.107Z`.

**Why the sliding window is the one a capacity claim must use.** A peak is an
upper-bound statistic. Fixed calendar-second buckets split any burst that
straddles a boundary, so they systematically **understate** it — and
understating a peak is the dangerous direction. The three figures are not in
conflict; they are progressively less lossy views of one burst.

**This makes the capture tooling's peak computation a DEMONSTRATED MEASUREMENT
DEFECT**, not merely a differing convention: it reports a capacity-relevant
upper bound that is **21% low**, and a research or operator report reading
`frames_per_second_peak_1s` would inherit that. It qualifies for repair under
the standing rule. `docs/evidence/KALSHI-PROD-QUAL-CAPTURE-2-capture.json` is a
frozen record of what the tooling computed and is **deliberately not edited**;
the defect is in the tooling, not in the record of it.

**Downstream figures that move.** Peak ÷ mean burst factor **≈ 4.4×**
(612 / 140). Headroom under the ~6,900 f/s closer ceiling **≈ 11×**, not the 12×
of §16.2.1 nor the 14× first stated.

**The prior's status is unchanged and is the point:** `~500 events/s` was an
*assumption* used to size `DEFAULT_MAX_SEGMENT_RECORDS`, and the measured peak
exceeds it. The sizing input was **low, not conservative**. L20 still binds —
one ten-minute overnight window is not a peak-capacity estimate, and the true
daily peak is very likely higher than 612.

---

## §16.4 — the sequence domain is per-SID, measured on production

The P4 tape carries four subscription identities, and each channel has its own:

| sid | channels carried | frames | `seq` |
|---|---|---:|---|
| 1 | `orderbook_delta`, `orderbook_snapshot` | 79,256 | **1 … 79,256, perfectly contiguous** |
| 2 | `ticker` | 2,395 | **always null** — unsequenced |
| 3 | `trade` | 2,516 | present, own domain |
| — | `subscribed` (control acks) | 3 | null |

**L22 (binding). `seq` is global within a SID, not within a connection.** Each
channel is subscribed separately and gets its own counter. This is the
production confirmation of the DEMO-derived claim in §4, and it sharpens it: the
sequence domain is `(session, subscription_generation, sid)` — the sid component
is load-bearing, not decorative.

**Consequence for B3, recorded so it is not re-litigated.** It is tempting to
read the 2,516 skipped `trade` frames as B3's production surface, because
`replay()` skips them through the same `continue` and they *do* carry a non-null
`seq`. **They are not.** They live on sid 3, where replay never builds a router
at all, so no orderbook sequence can be perturbed by skipping them. The
orderbook sid replays **1 … 79,256 with zero gaps, zero faults, zero refusals,
and every book publishable** — measured, not asserted. B3's surface on this tape
is genuinely **zero**, and B3 must continue to be closed against a wire-faithful
`error`-frame fixture rather than against production evidence.

**Snapshot behaviour.** 13 snapshots for 12 markets: one market
(`KXMLBGAME-26AUG191805MIAPHI-PHI`) was re-snapshotted **inside a single
generation**, with `connection_generation` and `subscription_generation` both
constant at 1 for all 84,170 records. A mid-generation re-snapshot is ordinary
venue behaviour and must not be read as a reconnect.

### §16.4.1 — root causes of the P4 readout defects, from this tape

Each of the reported figures is contradicted by the tape, and three of the four
causes are now identified rather than merely observed:

| readout reported | tape measures | cause |
|---|---|---|
| `subscription_generations: 3` | **1** distinct generation | **counts SIDS (3), labels them generations** |
| `segments_committed: 1` | **7** distinct `segment_id` | undercount — commit accounting |
| `healthy: false` | all 12 books `publishable`, 0 faults | readout, not state |
| claim path `ROOT/production/` | on disk `ROOT/env=production/` | partition prefix dropped |

**ALL FOUR REPAIRED 2026-08-19 (P4.2), each pinned by a test proven red against
the pre-fix code.** `segments_committed` is now `rotations + len(close())` = 7;
`subscription_generations` now reports `subscription_epoch` (1) with the router
count preserved as `subscription_router_epochs` (3); the terminal-state readout
gained a typed `liveness` of `NOT_APPLICABLE:carries_no_orderbook`, leaving
`healthy` untouched for the book model.

**The B4 path was not a readout defect at all — it was a guard that guarded
nothing.** `session_root.env_root` returned `root / environment` while the
archive writes to `env={environment}`, so a second concurrent session would
never have collided with the claim and BOTH would have succeeded while the
boundary reported itself enforced. The P4 tape root shows both directories side
by side.

The other three are **readout** defects: the archived evidence is correct and
the replayed state is correct in every case. That is precisely why they are dangerous — a
research consumer reading the summary rather than the tape inherits a wrong
generation count, a wrong segment count, and a false unhealthy flag, from a
capture that was in fact clean. Repair is scoped to the reporting path; the raw
tape is not to be touched.

---

## §18 — L23: the event time is `occurrence_datetime`, not `close_time`

**L23 (binding).** A Kalshi market carries **two unrelated times**, and the one
whose name suggests "when this market's life ends" is not the one that governs
when it is active.

| field | what it actually is |
|---|---|
| `occurrence_datetime` (== `expected_expiration_time`) | **the underlying event** — first pitch, match start. Activity tracks this. |
| `close_time` (== `expiration_time`, `latest_expiration_time`) | a **settlement deadline**, typically *days* later |
| `open_time` | when trading opened — often days *before* the event |

Measured on the live production book:

* `KXMLBGAME-26AUG222040MINSD-SD` — event `2026-08-23T03:40Z`, `close_time`
  `2026-08-26T00:40Z`. **Three days apart.**
* `KXATPMATCH-26AUG20TIRFIL-TIR` — event on 26 Aug 20, `close_time`
  `2026-09-03`. **Two weeks apart.**

**How this was found, because the failure mode is the point.** The first
`PROD-ACTIVITY-PROFILE-001` universe rule selected markets "closing on the
profile day" using `close_time`, and returned **zero candidates for both
days** — a loud, unambiguous failure. Had the two fields been merely *close*
rather than days apart, it would instead have returned a plausible-looking
universe of markets whose events were elsewhere in the week, and the entire
activity profile would have measured near-silent instruments while reporting a
full 40-market universe. This is the recurring class in one sentence: **the
field name was evidence of nothing, and the benign-looking answer was the
dangerous one.**

**Consequences for anything that selects a universe.**

1. Select on `occurrence_datetime`. `close_time` answers a different question.
2. A market being `status=open` says nothing about whether it is *active* —
   markets are listed days ahead and are near-silent until their event
   approaches. Openness is necessary, not sufficient.
3. **Time-of-day and time-to-event are distinct axes and are easy to
   conflate.** A universe held fixed across two days is, for event-driven
   instruments, a universe whose activity is dominated by proximity to its
   event rather than by the hour of the day. That is why
   `PROD-ACTIVITY-PROFILE-001` freezes its universe **per day** (Amendment 2).
4. **Same-day settlement means market identity does not persist.** Every
   sampled market from the P4 tape was already `NOT OPEN` ~6 h after capture.
   Cross-day comparison must therefore be made at **series** level; only
   within-day comparison can use market identity.
