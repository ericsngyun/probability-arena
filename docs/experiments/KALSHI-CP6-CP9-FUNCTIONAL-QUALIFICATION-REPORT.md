# CP6–CP9 — functional qualification on Kalshi DEMO

**Branch `KALSHI-CP6-CP9-FUNCTIONAL`. Not merged.**
Scope governed by §8 of `KALSHI-CP6-CP9-QUALIFICATION-PREREGISTRATION.md`.

> **ADDENDUM 2026-08-17 — the CP7 failure is FIXED, on a branch, and this
> document is NOT rewritten.** Everything below remains the record of what the
> code did during the three live sessions, which is what an experiment report
> is for. What changed since: `KALSHI-REPLAY-GENERATION-CONSISTENCY-001` made
> publishability per-market and generation-aware, and the strict-xfail claim
> this report leaves behind in §3.3 now passes with its marker removed.
>
> **Would CP7 pass now?** Its third property — per-market independent
> re-acquisition — now holds, proved on the venue's own frames from
> `s2-reconnect` in `tests/test_kalshi_replay_generation_consistency_001.py`
> (25 tests, including the revert control that turns 17 of them red). The other
> two CP7 properties are untouched and still hold. **But CP7 is a LIVE
> qualification and this is an offline proof over its captured frames**: the
> verdict below is only retired by a new live reconnect session, which this
> addendum does not claim to have run. Nothing else in the verdict block moves —
> throughput, latency, capacity and microstructure realism are exactly as
> unestablished as they were.

---

## 0. The verdict

```
Collector semantics      QUALIFIED
Reconnect behavior       FAILED
Archive conservation     QUALIFIED
Replay equality          QUALIFIED
Fault isolation          QUALIFIED

DEMO throughput          NOT QUALIFIED
Production latency       NOT MEASURED
Production capacity      NOT MEASURED
Microstructure realism   NOT ESTABLISHED
```

**The four lower lines are the scope of the claim, not caveats.** Nothing in
this document supports a rate, latency-tail, throughput, capacity or
microstructure statement, and no frame count here should be read as one. DEMO
is an engineering sandbox: 98.3% of the frames its eligible population emits
come from 194 venue test instruments, and this qualification uses 60 of them
**deliberately** — a functional proof needs frames that exercise the code
paths, not frames that resemble production activity. Rate and capacity belong
to `KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001`.

**One correctness property does not hold.** CP7's per-market requirement is
violated on the live venue, at both forced generation boundaries, for 59 of 60
markets. It is stated in §3 and it is the reason `Reconnect behavior` is
FAILED rather than qualified-with-a-note.

**Where a line was drawn, and where you may want to draw it differently.** §4 of
the preregistration says "any state that cannot be reconstructed from the
durable tape is a QUALIFICATION FAILURE". Two quantities did not reconstruct
(§4.5): the per-subscription `recoveries` counter, and the epoch-advance counter
on the unsequenced `ticker` sid. Both are **records of collector actions**, not
market state; every book, every checksum, every publishability flag and every
ordering finding on every sequenced sid reconstructed exactly, in all three
sessions. On that reading — the tape is a record of what the venue said, and
what the venue said is fully conserved — `Archive conservation` and `Replay
equality` are QUALIFIED. A reader who holds that the tape must also record what
the *collector* did will read those two lines as FAILED, and §4.5 gives the
numbers to do so without re-running anything. The fix is small and is
recommended in §9 either way.

---

## 1. What ran

Three sessions, read-only, on 2026-08-17, from a throwaway clone on EVO whose
production checkout stayed clean at `1549984` before and after. Market-data
channels only (`orderbook_delta`, `ticker`, `trade`), all inside
`kalshi.ALLOWED_CHANNELS`. The only frames that reached a socket were the
collector's own `subscribe` and, in the drop session, its own
`update_subscription` recovery.

| session | duration | perturbation | frames | archived | rejected | malformed | faults | recoveries | error frames |
|---|---:|---|---:|---:|---:|---:|---:|---:|---:|
| `s1-observe` | 180 s | **none** (the negative control) | 9,309 | 9,309 | 0 | 0 | **0** | **0** | **0** |
| `s2-reconnect` | 180 s | 2 forced socket teardowns | 8,599 | 8,599 | 0 | 0 | **0** | 0 | 0 |
| `s3-drop` | 120 s | 1 withheld orderbook frame | 6,488 | 6,488 | 0 | 0 | **14** | **1** | 0 |

Universe: the same 60 venue test instruments the P0 wire capture used, frozen
before any socket was opened. Artifacts: `KALSHI-CP6-CP9-FUNCTIONAL-RUNS/`.
Instruments: `scripts/kalshi_cp6_cp9_functional_probe.py` (live) and
`scripts/kalshi_cp6_cp9_conservation_check.py` (offline, pure). Both were
written and committed **before** any session ran; the wire recorder is imported
verbatim from the P0 probe so CP6 does not re-answer the P0 questions with a
differently-behaved instrument.

The harness was proven offline first, against the venue's own captured bytes:
`tests/test_kalshi_cp6_cp9_functional_001.py`, 22 passing + 1 strict xfail.

---

## 2. CP6 — live semantics. QUALIFIED.

### 2.1 Channel / sid assignment — replicated on a fresh socket

The venue's own `subscribed` acks, verbatim from `s1-observe`:

```json
{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":1}}
{"id":1,"type":"subscribed","msg":{"channel":"ticker","sid":2}}
{"id":1,"type":"subscribed","msg":{"channel":"trade","sid":3}}
```

Identical to the P0 capture. The ack still carries **no top-level `sid`** — the
sid is inside `msg` — so an ack is not routable to a subscription and cannot be
the source of this mapping in code. The collector derives it from frames
instead (`SubscriptionState.carries_orderbook`), and the terminal state shows
that working: sid 1 `carries_orderbook=True` with 60 books; sids 2 and 3
`carries_orderbook=False` with **zero** books, so no false publishability
exists on a channel that has no book.

`s2-reconnect` adds the fact that matters for CP7: across three subscribes the
venue re-issued **the same sids 1/2/3 every time** (9 acks, ids 1, 2, 3). Sid
alone therefore cannot separate the epochs — the generation stamp is
load-bearing, not decorative.

### 2.2 Sequence domains — measured per sid, not assumed

`s1-observe`:

| sid | channel | frames | `seq` present | contiguous | gaps | dups | regressions |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `orderbook_delta` (+ snapshots) | 7,311 | 7,311 (1…7,311) | 7,310 | **0** | 0 | 0 |
| 2 | `ticker` | 1,852 | **0** | — | — | — | — |
| 3 | `trade` | 143 | 143 (1…143) | 142 | **0** | 0 | 0 |
| — | `subscribed` acks | 3 | 0 (and no `sid`) | — | — | — | — |

Three separate sequence spaces, each clean. The trade sid ran 1…143 with no
hole and the collector reported **zero** faults on it — the P0 defect
(`faults = trades + 1`) does not recur on a live archiving session.

### 2.3 Subscription generations

`s1-observe` held one epoch and stamped it on all 9,309 records
(`tape_subscription_generations = {1: 9309}`, zero records with an unknown
generation). `s2-reconnect` held three and stamped
`{1: 600, 2: 600, 3: 7399}` — the tape's epochs equal the epochs the collector
held, and none is `null`.

**The epoch earns its keep, measured.** In `s2-reconnect`, a naive per-sid
ordering census that ignores generation reports **999 apparent regressions and
2 apparent duplicates on sid 1**, and 9 apparent regressions on sid 3 — because
`seq` restarts at 1 in every generation. The generation-aware live lane
reported **0 sequence faults**, and the generation-aware replay lane reported
**0 faults**. Without the epoch, two forced reconnects would have manufactured
1,008 false faults on a stream that lost nothing.

### 2.4 Snapshot forms — typed absence, on live data, at scale

360 snapshots across the three sessions. The census separates *key absent* from
*key present holding an empty list*, so the two can never be merged:

| observed | `s1` (60) | `s2` (180) | `s3` (120) |
|---|---:|---:|---:|
| both ladders present | 12 | 171 | 96 |
| one side's key **absent** | 45 | 0 | 18 |
| **both** sides absent | 3 | 9 | 6 |
| a side present but **empty** | **0** | **0** | **0** |

48 of `s1`'s 60 snapshots omitted at least one side — the case that used to be
collapsed into "present with zero levels" is the common case here, not an edge
case. The typed state survives into the books: `s1`'s terminal state carries
`yes: omitted_by_venue` on 30 of 60 markets and `no: omitted_by_venue` on 21
(exactly the census totals), never a level count of zero standing in for "the
venue said nothing".

**Not established: the `EMPTY` state.** In 360 live snapshots the venue never
sent a ladder key holding an explicitly empty list. `LadderState` has three
states and only two of them — `NOT_PROVIDED` and `PRESENT` — were exercised by
the venue. The `EMPTY` branch is covered by fixtures only, and this report
claims nothing about it from live evidence.

### 2.5 Normalized representation

Every archived record's `normalized` block was re-derived from its own archived
`raw`, using the same `normalize_frame` and the record's own
`collector_receive_time` (the resolution block is a function of it). **9,309 of
9,309 matched byte for byte**, across all five event types including the
`subscribed` control frames — `normalize_frame` is total, and the archive shows
it. Comparison is type-preserving, so a silent `Decimal` → `str` change would
have registered as a mismatch rather than as equality; zero recomputed
observations were rejected by the archive's own canonical encoder.

The negative control fired: corrupting one stored `normalized.event_type`
produced exactly one mismatch. A conservation check that cannot fail is not
evidence.

### 2.6 Zero unexplained recovery errors

`s1-observe`: **0 error frames**, **0 recoveries requested**, and exactly one
command on the wire (`subscribe`). `s2-reconnect`: 0 error frames, 0
recoveries, three commands (one `subscribe` per connection). `s3-drop`: 0 error
frames and exactly one recovery — an `update_subscription` aimed at sid 1, the
orderbook sid, sent once for one gap rather than once per faulting frame.

The P0 failure mode — a recovery aimed at the trade sid, answered
`{"code":13,"msg":"Unsupported action"}`, consuming a sequence number and
manufacturing the next fault — did not occur in any session.

**No semantic mismatch was found, so the CP6 stop rule did not fire.**

---

## 3. CP7 — reconnect correctness. FAILED.

Two of the three required properties hold. The third does not.

### 3.1 `generation_after > generation_before` — PROVEN

Two forced teardowns of the **real** socket (`ForceCloseTap` calls the
transport's own `close()`, so the collector's own read raises its own
`TransportError` and walks its own reconnect ladder — the boundary is produced
by the code path a venue disconnect drives, not by a mocked exception).

```
forced close #1  after 600 frames   17:42:05Z
forced close #2  after 600 frames   17:42:26Z
```

`subscription_epoch` 1 → 2 → 3. `connection_generation` 1 → 2 → 3.
`reconnects = 2`, `disconnects = 2` in the metrics lane. Three transports were
built, three subscribes were sent, nine acks came back. The paired control:
`s1-observe`, same universe, no forced close, ended at `subscription_epoch = 1`
and `reconnects = 0` — the counter is not pinned high.

### 3.2 A genuine within-generation gap still faults — PROVEN

The anti-vacuity control, and the one that mattered most: the P0 fix separated
ordering from basing, and a fix that makes a fault counter read zero is
indistinguishable from a fix that broke the counter.

One `orderbook_delta`, sid 1, **seq 401**, market `…T62699.99`, was withheld
from the collector inside generation 1. That is exactly what a venue drop looks
like from the collector's side.

| | observed |
|---|---|
| wire census gap example | `previous: 400, observed: 402` — a hole of exactly one |
| metrics `sequence_gaps` | 0 → **1** |
| session `sequence_faults` | **14** (the gap, then 13 deltas refused while the subscription awaited its new base) |
| recoveries requested | **1** — one command for one gap, not a storm |
| command sent | `update_subscription`, sids `[1]`, at frame 413 |
| error frames | 0 — the venue accepted it |
| books unpublished | **all 60 on sid 1**, in one step, at seq 402 |
| books re-acquired | **60 separate re-acquisitions, each on its OWN recovery snapshot** |
| sid 3 (`trade`) through the event | 1…80, 0 gaps, unaffected |

The paired control is `s1-observe` on the same universe: `sequence_gaps = 0`,
`sequence_faults = 0`, `recoveries_requested = 0`.

### 3.3 Per-market independent re-acquisition — **VIOLATED**

The preregistration requires, independently for every market:

> `old book → nonpublishable → its own new snapshot → publishable`,
> and **no book may silently survive across a generation boundary as if
> nothing happened.**

It does not hold. The publishability transition timeline from `s2-reconnect`,
unedited:

```
frame    4 … 63   epoch 1   60 SEPARATE entries, one per market,
                            each caused by THAT market's own snapshot (seq 1…60)
frame  601        epoch 2   subscribed ack → 60 markets  True → False   (one entry)
frame  604        epoch 2   orderbook_snapshot seq=1 for T55099.99
                            → 60 markets  False → True   (ONE entry)
frame 1201        epoch 3   subscribed ack → 60 markets  True → False
frame 1204        epoch 3   orderbook_snapshot seq=1 for T55099.99
                            → 60 markets  False → True
```

At frame 604 exactly one market had been re-snapshotted. **The other 59 were
republished on a sibling's snapshot**, still carrying ladders from the epoch
the venue had just abandoned. The same thing happened again at frame 1204.

(The `subscribed` frames at 601 and 1201 did not *cause* the unpublish —
`_begin_subscription_epoch()` runs as soon as the resubscribe is accepted by the
socket, before any frame of the new generation can be read. They are simply the
first frames at which the observer could sample the change. The collector's
ordering is correct; it is the re-acquisition that is not.)

Contrast the cold start in the same session — 60 separate per-market entries —
and the fault path in `s3-drop` — 60 separate per-market re-acquisitions. The
collector achieves per-market independence on both of those. It loses it
**only** at a generation boundary.

**Mechanism.** `publishable_books()` is
`book.publishable AND subscription.healthy`. `healthy` is a **subscription**
flag, and the first snapshot of the new generation sets it for the whole
subscription. Meanwhile `begin_subscription_generation()` deliberately rebases
each book *without* unpublishing it — it clears `last_seq` and nothing else —
so every un-resnapshotted book still carries `synced=True` and no integrity
reason from the previous epoch. The conjunction therefore flips all of them
back to publishable at once. This is `KALSHI-REPLAY-GENERATION-CONSISTENCY-001`
(generation-aware `publishable_books()`), which §6 of the preregistration
schedules as the **next** work item — and §3 states explicitly that deferring
it "does not excuse the live collector from this proof."

**How far the harm went in these sessions — measured, not assumed.** Generation
1 is the cold start, where the books do not yet exist and the timeline shows 60
independent acquisitions; only generations 2 and 3 are boundaries:

| generation | markets publishable on a **previous** generation's ladder | max records before a market's own snapshot | wall-clock span | **new-generation deltas applied to an abandoned ladder** |
|---:|---:|---:|---:|---:|
| 2 | 59 of 60 | 59 | 36 ms | **0** |
| 3 | 59 of 60 | 59 | 36 ms | **0** |

So the observed consequence was bounded: a ~36 ms window in which up to 59
markets served a stale-but-uncorrupted top of book, and **not once** did a
new-generation delta land on an abandoned-generation ladder. That is the
serious case — a price-level change from epoch *N* applied on top of epoch
*N−1*'s ladder fabricates a book rather than merely serving a stale one — and
it did not occur here.

**It did not occur because the venue happened to send all 60 snapshots
contiguously before any delta. That is not a contract we hold.** The guard that
would make it impossible is the one that is missing. A larger universe, a
slower venue, an interleaved snapshot stream, or a market that simply never
re-snapshots all move the exposure, and in the last case the stale book stays
published indefinitely.

The property is pinned as an executable claim in
`tests/test_kalshi_cp6_cp9_functional_001.py::TestForcedSocketTeardown::test_each_market_regains_publishability_only_on_its_OWN_new_snapshot`,
marked `xfail(strict=True)`: the day the fix lands it XPASSes, the suite fails,
and this verdict must be revised on evidence rather than remembered.

**That happened, 2026-08-17.** The fix landed on branch
`KALSHI-REPLAY-GENERATION-CONSISTENCY`, the test XPASSed and failed the suite
exactly as designed, and its marker was removed. The mechanism named above is
the one that was repaired: `publishable_books()` no longer ANDs a per-book flag
with a subscription-level `healthy`, and a book is publishable only while
`based_generation == subscription_generation` — i.e. only after **its own**
snapshot for the current epoch. A new-generation delta arriving on an
un-re-snapshotted book is now refused rather than applied, which is what closes
the "not a contract we hold" exposure in the paragraph above rather than merely
narrowing it. The proofs are re-run against `s2-reconnect`'s own verbatim frames
in `tests/test_kalshi_replay_generation_consistency_001.py`. **The live sessions
were not re-run**; see the addendum at the top of this document for what that
does and does not retire.

---

## 4. CP8 — deterministic replay and conservation

Eleven checks per session, run offline against the digest-verified tape. Two of
them are negative controls that must FAIL under corruption or the whole check
is vacuous.

| check | `s1-observe` | `s2-reconnect` | `s3-drop` |
|---|---|---|---|
| archive integrity intact | PASS | PASS | PASS |
| raw-frame conservation | PASS | PASS | PASS |
| normalized-frame conservation | PASS | PASS | PASS |
| — negative control (corrupted `normalized`) detected | PASS | PASS | PASS |
| generation conservation | PASS | PASS | PASS |
| per-sid ordering census conserved (wire ↔ tape) | PASS | PASS | PASS |
| `ladder_presence` conserved | PASS | PASS | PASS |
| replay deterministic across two runs | PASS | PASS | PASS |
| **per-market terminal state equality** | **PASS** | **PASS** | **PASS** |
| — negative control (corrupted delta `seq`) detected | PASS | PASS | PASS |
| subscription counters reconstructible from tape | PASS | **FAIL** | **FAIL** |

**Reading this table against the raw JSON.** The `-cp8.json` artifacts carry a
`checks.state_equality` boolean that folds the per-market comparison *and* the
per-subscription counter comparison into one bit, so it reads `false` for `s2`
and `s3` and the file's own `cp8_verdict` reads `FAILED`. This table splits
that bit, because the two halves are different claims about different things:
the market row is `state_equality.differences == []` (empty in all three
sessions) and the counter row is the `recoveries` divergence of §4.5. The raw
files are the evidence and are deliberately the stricter of the two.

### 4.1 Raw-frame conservation

For every session: `wire frames tapped == events_received == events_archived ==
records on disk == records the integrity check verified`, and
`received == archived + rejected + malformed` held exactly. Per-event-type
counts agreed between the socket tap and the file with **zero** differences.
`s1`: 9,309 everywhere. `s2`: 8,599. `s3`: 6,488 (the withheld frame is absent
from the tap and from the tape alike, which is what makes the arithmetic
meaningful rather than merely consistent).

### 4.2 Normalized-frame conservation — where normalization is defined

`normalize_frame` is total: it produces a full coverage map for every frame,
including event types it has never seen. So "where normalization is defined" is
**every record**, and every record was checked: 9,309 / 8,599 / 6,488, all
byte-identical, all canonically encodable.

### 4.3 Generation conservation

Every record in every session carried a non-null `subscription_generation` and
`connection_generation`. The set of epochs on the tape equals the set the
collector held: `{1}` for `s1` and `s3`, `{1, 2, 3}` for `s2`. Forging a
generation the collector never reached is detected (asserted in the tests).

### 4.4 State equality — `State_live^terminal == State_replay^terminal`

**180 of 180 market-sessions matched.** All 60 per-market checksums, all 60
publishability flags and all 60 per-market stat blocks were identical between
the collector's own terminal routers and a replay of its tape — in the clean
session, in the session with two generation boundaries, and in the session with
a real sequence gap and a venue recovery. Replay produced bit-identical output
on two consecutive runs of every tape.

| | applied | rejected | replay faults | live `sequence_faults` |
|---|---:|---:|---:|---:|
| `s1-observe` | 7,311 | 0 | 0 | 0 |
| `s2-reconnect` | 6,781 | 0 | **0** | **0** |
| `s3-drop` | 4,937 | 14 | **14** | **14** |

The last row is the one worth reading twice. Replaying the tape of the forced
gap reproduced the fault count **exactly** — fourteen live, fourteen on
replay — and still arrived at the same 60 terminal books, because the venue's
recovery snapshots are on the tape and re-base the books the same way on both
lanes. The reconnect tape, by contrast, produced zero faults on both lanes:
replay agrees with the collector that two generation boundaries are boundaries
and not losses.

Notably, replay reproduces the CP7 defect faithfully — the reconnect session's
replayed `publishable` map matches the live one, defect included. That is the
correct behaviour for a replay lane: it must reproduce what happened, not
improve on it.

`ladder_presence` is compared separately and also matched 60/60 in every
session. This is not implied by the checksum: `OrderBook.checksum()` digests
`{market_ticker, generation, last_seq, sid, yes, no}` and **not**
`ladder_presence`, so an equal checksum is no evidence that a typed absence
survived the round trip. Doctrine 10 is exactly that distinction, so it is
measured on its own.

### 4.5 What the tape cannot answer — stated, not omitted

Two quantities did not reconstruct. **Neither is market state, and neither
affected a single book**, but both are the repo's characteristic failure shape —
a plausible benign value produced by a path that cannot know — so they are
recorded here in full.

**(a) `SubscriptionState.stats["recoveries"]` is not derivable from the tape.**

| session | sid | channel | live | from tape | every other field |
|---|---:|---|---:|---:|---|
| `s2-reconnect` | 1 | `orderbook_delta` | 2 | 0 | identical |
| `s2-reconnect` | 2 | `ticker` | 2 | 0 | see (b) — `generation_advances` also differs |
| `s2-reconnect` | 3 | `trade` | 2 | 0 | identical |
| `s3-drop` | 1 | `orderbook_delta` | **14** | **0** | identical |

Those four rows are the complete set of differences the checker found across
all three sessions — `s1-observe` had none.

`recoveries` counts a **collector action** — `begin_recovery()`, called when the
collector decides to resynchronise or when `supersede()` runs on a resubscribe.
The archive is a record of inbound venue messages; the outbound
`update_subscription` is not archived, and nothing in the tape marks the moment
the collector chose to send it. So a reader replaying `s3-drop` would conclude
the collector resynchronised **zero** times when it resynchronised fourteen.
Every other field — `accepted`, `gaps`, `duplicates`, `regressions`,
`wrong_sid`, `stale_generation`, `missing_seq`, `generation_advances` — matched
exactly on every sequenced sid, `trade` included.

**(b) On the unsequenced sid, the epoch-advance counter is not derivable.**
On sid 2 (`ticker`) the live subscription recorded `generation_advances = 2`
and the tape-derived one recorded 0, because `dispatch` can only advance an
epoch on a frame that carries a `seq` and no ticker frame does. The per-record
generation **stamps** are all present and all conserved — a reader can always
say which epoch any individual ticker record arrived under. What is lost is
only the per-subscription counter.

**Recommended before `MARKET-MICROSTRUCTURE-EDGE-001` treats a recovery count
as data:** archive the outbound command (or a typed recovery marker) so the
tape can answer "did the collector resynchronise, and when", or move
`recoveries` out of `SubscriptionState.stats`, which currently mixes a property
of the stream with a record of an operator action.

### 4.6 A scope limitation of the shipped `replay()`

`archive.replay()` returns early on every non-orderbook event type, so it never
builds a router for the `trade` or `ticker` sid — it reported one subscription
where the live lane held three. The tape is **not** the limitation:
`subscription_findings_from_tape` re-derives every sid with the same
`SubscriptionRouter` the live lane used and reproduces the live ordering
findings exactly, `trade` included (`accepted: 115` on sid 3 in `s2`, matching
live). The gap is in the replay function's scope, and it is pinned by a test so
that widening `replay()` retires this note on evidence.

### 4.7 `ticker` — explicitly, what can and cannot be established

The venue sends `ticker` with **no `seq` at all**: 1,852 of 1,852 in `s1`,
1,694 of 1,694 in `s2`, 1,454 of 1,454 in `s3` — replicating the P0 capture's
2,071 of 2,071 on three fresh sockets.

**Established for `ticker`:**

* every ticker frame the collector received is on disk, counted in raw-frame
  conservation, and present in the per-type census agreement between the socket
  tap and the file;
* every ticker frame re-normalizes byte-identically from its archived `raw`;
* every ticker record carries the subscription and connection generation it
  arrived under.

**Not establishable, and not by any amount of further work on this tape:**

* **whether any ticker frame was lost between the venue and the collector.**
  There is no ordering field, so no gap, duplicate or regression finding exists
  to conserve. `seq_gaps = 0` on sid 2 is an arithmetic artefact of an empty
  domain, not an observation, and the checker carries a typed
  `ordering_findings_establishable: false` beside it precisely so that zero is
  never read as a clean measurement;
* therefore **no completeness claim for `ticker`**, and no feature derived from
  it — a rolling ticker volume, a quote-update rate, anything — may be
  described as lossless. It inherits "no sequence-based loss detection" from
  its source;
* the per-subscription epoch-advance counter for that sid (§4.5b).

A drift detector is in the suite: if Kalshi ever starts sequencing `ticker`,
`test_the_ticker_channel_still_carries_no_sequence_number` fails, and this
caveat gets **retired on evidence** instead of being carried forever.

---

## 5. Fault isolation. QUALIFIED.

Forced in `s3-drop` and observed on three axes:

* **Across subscriptions.** The gap on sid 1 unpublished all 60 books on sid 1 —
  correct, since the lost message could have belonged to any of them — and left
  sid 3 untouched: `trade` ran 1…80 with zero gaps straight through the event.
* **Across the session.** Fourteen faults did not end the session; it ran to its
  own time cap with `events_rejected = 0`, `frames_malformed = 0`,
  `rotation_failures = 0`, and every frame archived **before** validation, so no
  faulting frame cost us its record.
* **Across the measurement lane.** `metrics_errors = 0` and `observe_errors = 0`
  in all three sessions; the metrics lane's `sequence_gaps` moved 0 → 1 on the
  forced gap and stayed at 0 in the control, so it is wired to the thing it
  claims to count.

The recovery was contained too: one command for one gap, aimed by observation
(`carries_orderbook`, set from frames) at the only sid that can accept it.

---

## 6. Deviations from the preregistration

Logged per §7, all forced by the §8 amendment or by DEMO's nature.

1. **Sample floor.** The ≥100,000-frame floor is withdrawn for DEMO by §8. The
   three sessions total 24,396 frames. **No rate, tail or capacity claim is
   made from them**, and the CP9 block says so in four lines.
2. **Universe.** §1's "12 live tickers in 4/4/4 message-rate strata" is not
   usable: `KALSHI-TAPE-MANIFEST-001` REFUSED the manifest because DEMO has an
   empty middle, and `KALSHI-DEMO-TRAFFIC-CAPACITY-001` found 98.3% of eligible
   frames come from test instruments. 60 venue test instruments were used
   instead — the same set the P0 wire capture used, frozen before capture. They
   are legitimate for a **functional** proof and worthless for a
   microstructure one; that is stated in `scope_note` on every artifact.
3. **CP6 and CP8 share one session.** `s1-observe` is both the semantics
   session and the conservation tape. They are separable questions answered
   from one unperturbed capture, which also serves as the negative control for
   the two perturbed sessions.
4. **CP7's reconnect is forced by tearing down the real socket** rather than by
   waiting for a venue disconnect. The teardown is genuine — the collector's
   own read raises, its own ladder runs, a new socket handshakes and
   resubscribes — but the *cause* is ours. Whether a venue-initiated disconnect
   behaves identically is not established.
5. **The live sequence gap is forced by withholding a frame**, not by observing
   a natural drop. No natural gap has ever been observed on any DEMO
   subscription. The gap the collector saw is genuine; its origin is not.

---

## 7. What this does NOT settle

1. **Production.** Every number is DEMO. Nothing here licenses a claim about
   production behaviour, and CP10's separate Tier-2 approval is unaffected.
2. **The `EMPTY` ladder state.** Never observed in 360 live snapshots; covered
   by fixtures only.
3. **Whether `get_snapshot` repairs a *real* loss.** `s3-drop` proves the
   command is accepted, answered with 60 snapshots, and that every book
   re-acquires publishability on its own. The frame that was "lost" was
   withheld by us, so the venue never had to find it.
4. **Whether a venue-initiated disconnect behaves like a forced one** (§6.4).
5. **The CP7 exposure window under other conditions.** Zero deltas landed on an
   abandoned ladder here because the venue sent all 60 snapshots contiguously.
   The bound is the venue's behaviour on the day, not a property of the
   collector.
6. **Rotation constants.** `DEFAULT_MAX_SEGMENT_RECORDS = 13_000` still rests on
   an assumed peak rate. Every session committed exactly one segment and
   started zero rotations, so nothing here bears on retuning it — that was
   always a rate question and rate questions are out of scope.
7. **`ticker` completeness** — permanently, per §4.7.

---

## 8. Safety

Read-only throughout. Market-data channels only, all inside
`kalshi.ALLOWED_CHANNELS` and checked by `assert_channels_allowed` at
`CollectorConfig` construction. **Six commands reached a socket in total**:
five `subscribe` (one for `s1`, three for `s2`'s three connections, one for
`s3`) and one `update_subscription` recovery. Both shapes are built by
`kalshi.build_*` and re-validated by the transport. No order, position,
portfolio, wallet or key-management surface was reached. No key
material, key id or signed URL was copied, printed or logged — the run
artifacts record a key-id fingerprint and nothing else. `app/` is unmodified on
this branch. Sessions ran from a throwaway `/tmp` clone on EVO which was
removed afterwards; EVO's production checkout was verified clean at `1549984`
before and after, and no process was left running.

Safety grep (`AGENTS.md`) over the three new files: one hit, a boundary
statement in a docstring. Over `app/`: unchanged, because `app/` is untouched.

### 8.1 Suite state, and the attribution of every failure

`pytest -q -p no:randomly`, this host: **5,148 passed, 13 failed, 6 skipped,
5 xfailed** in **24 m 13 s**. The recorded baseline is **5,132 / 7 / 6 / 4** in
12 m 49 s.

The deltas reconcile exactly: **+22 passed** and **+1 xfailed** are the new
harness file (§1); the 6 extra failures are **more members of the same
wall-clock/staleness class the baseline already carries**, surfaced by a run
that took nearly twice as long. Three independent checks, as in the P0 finding:

1. **All 13 pass in isolation** — 16 tests, **16 s**, green.
2. **Every visible assertion is a duration bound blown by elapsed time**, not a
   behavioural difference: `market_freshness_s` 1130.5 against a `30…600`
   bound; `market_quote_age_s` 1593.6 against `120…900`;
   `age_minutes == approx(104, abs=1)`; `max_age_minutes <= 60.1`; and packets
   reporting `stale_market_quotes` where the test seeded them fresh. Each is a
   fixture seeded at a fixed offset from module import and asserted 24 minutes
   later. The one non-obvious member,
   `TestRealProcessKillMidWrite`, fails with *"SIGKILL landed after the writer
   opened its files, not before"* — a timing race against a real child process,
   and load-sensitive for the same reason.
3. **None of the seven files imports anything this branch adds**, and this
   branch adds no `app/` change at all: the complete diff against `a656909` is
   three new files plus documents.

**The elapsed time is partly my own doing and is stated rather than glossed:**
the full run overlapped two targeted `pytest` invocations of the new harness
file and several `ssh`/`scp` transfers to EVO on the same host. The class is
known to widen with suite duration — the baseline itself records `assert
120 <= 943.1 <= 900` from a 12 m 49 s run — so a 24 m run producing more of the
same members is the predicted behaviour, not a new signal. It is also a
standing weakness: these tests assert against wall clock rather than an
injectable clock, so the suite's own runtime is an uncontrolled variable in
them.

---

## 9. What should happen next

1. ~~**Fix CP7 before any microstructure feature reads a book across a
   reconnect.**~~ **DONE 2026-08-17** on branch
   `KALSHI-REPLAY-GENERATION-CONSISTENCY`, unmerged: publishability is now
   per-market and generation-aware, the strict xfail passes with its marker
   removed, and six proofs run over `s2-reconnect`'s verbatim frames. Still
   open, and deliberately: **a live reconnect session has not been re-run**, so
   the FAILED verdict in §0 stands as a record of the sessions that were.
2. **Decide whether the tape must record collector actions** (§4.5) before a
   recovery count becomes an experimental variable.
3. **Consider widening `archive.replay()`** to the non-orderbook sids (§4.6),
   or documenting in the schema freeze that it is orderbook-only.
4. Then the schema freeze, then `MARKET-MICROSTRUCTURE-EDGE-001`, per §6 of the
   preregistration.

The four lower lines of §0 stay `NOT QUALIFIED` / `NOT MEASURED` /
`NOT MEASURED` / `NOT ESTABLISHED` until
`KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001` runs.
