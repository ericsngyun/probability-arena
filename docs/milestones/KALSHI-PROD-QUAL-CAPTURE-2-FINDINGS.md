# KALSHI-PROD-OBSERVATIONAL-QUALIFICATION-001 — CAPTURE ATTEMPT 2

**2026-08-20. The production WebSocket host has been reached. §11 B1 CLOSES.**

```
Production semantics     QUALIFIED
Capture integrity        QUALIFIED
Observed production load 140.37 frames/s mean · 485 frames/s peak 1 s · 84,170 frames / 599.6 s
                         on 12 markets, 3 channels — the number DEMO could not give
Replay equality          NOT QUALIFIED — B3
```

Branch `KALSHI-PROD-QUAL-CAPTURE-2`, off `main` at `b0db246`. The collector ran
**exactly as shipped**: `git diff main -- app/` is empty. Nothing in `app/` was
edited by this milestone.

---

## 1. The production-evidence chain

Every link measured on a throwaway clone of `main` on EVO, before any frame was
accepted as production data.

| # | link | verdict | evidence |
|---|---|---|---|
| E1 | host constant | **PASS** | `WS_HOSTS[production]` = `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, the AsyncAPI-published host, and not the demo constant |
| E2 | DNS | recorded | production WS resolves to **8** addresses, demo WS to **2**, disjoint. Recorded, not asserted |
| E3 | TLS out of band | **PASS** | production WS `CN=*.kalshi.com`, SAN `["*.kalshi.com"]`, issuer *Amazon RSA 2048 M01*, TLSv1.3, verified against the default trust store with hostname checking on. Demo presents `CN=demo.kalshi.co`. Cryptographically distinct |
| E4 | credential identity **and scope** | **PASS** | `GET /trade-api/v2/api_keys` answered by **`api.elections.kalshi.com`**, HTTP **200**, key `sha256:cfdd78afeded1c22` present in that account, scopes **`["read"]`**, `proven_read_only: true` |
| E5 | TLS **on the capture socket** | **PASS** | handshake **101**, peer `16.58.202.54`, SAN `["*.kalshi.com"]`, zero demo names, `TLS_AES_128_GCM_SHA256` — read between handshake completion and the collector's first `recv()` |
| E6 | universe | recorded | all 12 tickers come from this session's own production REST census |

`verified_before_first_frame: true`. **Attempt 1's stop is cleared and the new
credential is read-only on the venue's own testimony**, re-verified here rather
than taken on trust.

**§11 B1 is now closed.** The production host was UNVERIFIED because
"documentation and a certificate are both stronger than a name, and neither is a
handshake." There is now a handshake: HTTP 101 on that exact URI, with the
certificate read off that same socket. `app/realtime/kalshi.py:52-55` may now be
updated to say so — **deliberately not done here**, because editing a source
comment is outside a capture phase's authority and belongs with the B1 closure.

---

## 2. The session

| | |
|---|---|
| manifest | frozen by this session — census 00:23:11Z→00:26:39Z, 487 pages, 208.1 s; probe 4 reads to 00:34:19Z |
| manifest verdict | **QUALIFIED**, 0 refusals. Frame 97,392 → 648 eligible (271 events / 119 series) → 12 selected |
| preflight | structural guard **CLEAN** (16 modules, 3,292 identifiers, 0 findings); root fresh; session `s-20260820T003520Z-f450f75ed1fc` claimed 00:35:20Z **before** the socket |
| archive root | `~/kalshi-prod-tape/p4-attempt2-20260820T003519Z` — one root, one session (§11 B4 run rule) |
| capture | 00:36:00.199Z → 00:46:01.437Z, `status=capped_time`, `detail="max_seconds=600 reached"` |
| outbound | **one** command: the `subscribe`. `commands_refused: 0`. No `get_snapshot` was needed |

**Chosen bound, stated: 600 s.** Production volume was unknown and no frame
floor applies to P4. 600 s was chosen because the semantic questions resolve in
the first seconds, while frame rate, burstiness and rotation need a contiguous
span with its quiet seconds included; and because a longer first run would
silently create a retention obligation that L15 says has no owner. It proved
ample: the run produced **6 rotations**, which DEMO never produced at all.

**A stale block in the manifest artifact, flagged not silently accepted.**
`session_parameters` still reads 2 h min / 100,000 archived frames / 4 h max.
Those were frozen by Eric for the **DEMO CP6–CP9** session. They do not govern
P4 and were not applied.

---

## 3. Production semantics vs the P3 contract — QUALIFIED

Re-verified against production because DEMO was measured as categorically
unlike it. **Every DEMO-taught semantic reproduced. No discrepancy was found,
so no stop was triggered.**

### 3.1 sid ↔ channel assignment — REPRODUCED

The three `subscribed` acks, verbatim:

| ack ordinal | `msg.channel` | `msg.sid` | top-level `sid` |
|---|---|---|---|
| 1 | `orderbook_delta` | **1** | **absent** |
| 2 | `ticker` | **2** | **absent** |
| 3 | `trade` | **3** | **absent** |

Identical to DEMO for the same three-channel subscribe. This **confirms P3 §3.1
rather than contradicting it**: the venue assigned sids in ack order for the
channels the subscribe named, and the ack order matched the request order. The
mapping is still a property of the subscribe, not a venue constant, and nothing
here licenses hard-coding it. The acks again carry **no top-level `sid`**
(sid lives inside `msg`), so they remain unroutable to a subscription — exactly
as §3.1 requires. The collector discovered the orderbook sid from frames, not
from the ack: `carries_orderbook` is `true` on sid 1 only.

### 3.2 Independent sequencing, and ticker unsequenced — REPRODUCED

Generation-blind per-sid census over the whole session:

| sid | channel | frames | `seq` present | first→last | contiguous | gaps | dups | regressions |
|---|---|---:|---:|---|---:|---:|---:|---:|
| 1 | orderbook | 79,256 | **79,256 / 79,256** | 1 → 79,256 | 79,255 | **0** | 0 | 0 |
| 2 | ticker | 2,395 | **0 / 2,395** | — | 0 | *empty domain* | 0 | 0 |
| 3 | trade | 2,516 | **2,516 / 2,516** | 1 → 2,516 | 2,515 | **0** | 0 | 0 |
| — | `subscribed` | 3 | 0 | — | — | — | — | — |

**Orderbook and trade are independently sequenced in production**: sid 1 ran to
79,256 while sid 3 ran to 2,516 over the same wall-clock, each perfectly
contiguous from 1. A shared sequence space would have manufactured tens of
thousands of faults.

**Ticker is unsequenced in production.** 0 of 2,395 frames carried a `seq`. The
drift detector's premise holds; L1 is unchanged.

The collector's own **generation-aware** numbers (authoritative, P3 §3.2) agree:
sid 1 `accepted: 79,256`, sid 3 `accepted: 2,516`, and on all three sids
`gaps / duplicates / regressions / wrong_sid / stale_generation / missing_seq /
recoveries / generation_advances` are **all 0**. One connection, one
subscription epoch, `subscription_generation = 1` throughout, 0 disconnects,
0 reconnects, 0 recoveries requested.

### 3.3 `error` frames consuming a seq — NOT OBSERVED, and stated as such

**Production sent zero `error` frames in 600 s.** So this run neither confirms
nor refutes that an `error` consumes a `seq` on the orderbook sid. **L8 remains
open**, exactly as it was after DEMO, and is recorded here as *not re-observed*
rather than as a pass. The conservative assumption in the code (it does consume
one) is untouched.

This is also why **B3 did not fire**: the defect needs a non-orderbook frame
carrying a `seq` on the replay path, and no `error` frame arrived. B3 is not
disproven — it was not exercised.

### 3.4 Snapshot ladder shape — REPRODUCED, including the state DEMO called common

13 snapshots, typed with the closed vocabulary (P3 §5.1, doctrine 10):

| state | count |
|---|---:|
| `yes=PRESENT no=PRESENT` | 10 |
| `yes=NOT_PROVIDED no=PRESENT` | 1 |
| `yes=NOT_PROVIDED no=NOT_PROVIDED` | 2 |
| **a side `PRESENT` but `EMPTY`** | **0** |

**3 of 13 production snapshots omitted at least one ladder.** The case that was
once collapsed into "present with zero levels" is real in production too.

**`EMPTY` has still never been observed live** — 0 in 13 production snapshots on
top of 0 in 360 DEMO snapshots. **L4 stays open.** n=13 is small and this is
weak evidence; it is not a retirement of the distinction.

The terminal book state is P3 §7.1/§7.2 conformant, and production produced the
state L4 predicted would matter: **two books are `publishable: true` with
`levels_yes = levels_no = 0`**, and the only field distinguishing that from a
genuinely empty book is `ladder_presence = omitted_by_venue`, which is present
and correct on both. §7.1's rule — *"a zero-level book is never observed
emptiness unless `ladder_presence` says so"* — is load-bearing on real
production data, not a hypothetical. A consumer reading `levels_*` without
`ladder_presence` would fabricate an empty book on 2 of 12 production markets.

### 3.5 Two further semantics settled from the tape

**P3 §5.4 — which field name supplied the value.** Production ticker quotes used
`yes_bid_dollars`/`yes_ask_dollars` on **2,395 of 2,395** frames; the bare
`yes_bid`/`yes_ask` spelling never appeared. Settled from the tape, as §5.4 said
the first real session would.

**P3 §5.1 — the `use_yes_price` / no-complement convention.** Across **2,405**
spread samples (10 full-ladder + 2,395 top-of-book) there were **0
locked-or-crossed** samples. Had the NO side needed complementing, crossed books
would have appeared. The convention holds in production.

Spread is reported as two never-pooled distributions, each from frames the
contract says carry one — full-ladder median $0.0100 (n=10); top-of-book median
$0.0100, max $1.0000 (n=2,395). No absence became a zero.

---

## 4. Capture integrity — QUALIFIED

The archive's own canonical verifier, run read-only against the tape on disk:

```
verdict          VALID          reasons  []        warnings  []
records_read     84,170         records_expected  84,170
truncated_records 0             head_state  CURRENT
segments 7       closed 7       open 0            invalid 0
```

**The session was not truncated.** All 7 segments closed cleanly and the head is
`CURRENT`; the final segment is committed, not left open. Every segment is
`valid` and `environment_valid`.

Conservation, end to end and independently counted on disk:

```
frames_received 84,170 = frames_yielded 84,170          (transport)
events_received 84,170 = events_archived 84,170          (collector)
append_calls    84,170
records on disk 13,000 x 6 + 6,170 = 84,170              (zcat | wc -l)
events_rejected 0   frames_malformed 0   frames_oversize 0   read_timeouts 0
sequence_faults 0   rotation_failures 0   observe_errors 0   metrics_errors 0
```

Per-sid conservation also closes: sid 1's 79,243 deltas + 13 snapshots = 79,256
= its frame count; 79,256 + 2,395 + 2,516 + 3 acks = 84,170.

---

## 5. Observed production load — the number DEMO could not give

### 5.1 Rates

| | measured |
|---|---|
| frames | **84,170** in **599.643 s** |
| mean | **140.37 frames/s** |
| **peak, 1 s bucket** | **485 frames/s** |
| median, 1 s bucket | 115 frames/s |
| **silent seconds** | **0 of 600** |
| burstiness (index of dispersion) | **54.55** (Poisson = 1.0) |
| interarrival | p50 **1.135 ms** · p90 17.31 · p95 31.18 · p99 92.14 · max 580.91 ms |
| bytes | 23,395,915 received; **~13.8 MB compressed on disk** |

Channel mix: `orderbook_delta` 79,243 (94.1%) · `trade` 2,516 (3.0%) ·
`ticker` 2,395 (2.8%) · `orderbook_snapshot` 13 · `subscribed` 3.
**The book feed is the load**; ticker and trade together are under 6%.

The stream is **continuous but strongly bursty** — not one silent second in ten
minutes, yet a dispersion index of 54.5 and a p50 interarrival of 1.1 ms against
a p99 of 92 ms. Sizing on the mean would understate the peak by 3.5×.

### 5.2 Rotation, append and the `DEFAULT_MAX_SEGMENT_RECORDS` verdict

Observed: **6 rotations**, segments closing at **exactly 13,000** records six
times and 6,170 for the final partial — one rotation per **~92.6 s**, matching
13,000 / 140.37 exactly. `rotation_failures: 0`.

**The constant's stated basis is vindicated, and the coordinator's read of it
must be corrected.** `segment.py:200-208` chose 13,000 to target a ~2 s close
and sized the rotation cadence against a **"~500 events/s assumed peak"**. The
comparable measured quantity is therefore the **peak**, not the mean:

| | |
|---|---|
| assumed **peak** | ~500 events/s |
| **measured peak, 1 s** | **485 events/s** |
| error | **~3% high — essentially correct** |
| measured **mean** | 140.37 events/s |
| assumption vs the **mean** | 3.6× high |

Comparing an assumed *peak* against an observed *mean* and concluding the
assumption was "3.5× conservative" would be the same class of error as reading
`strata_ranges.high_over_medium.ratio` as a venue cliff. **Verdict:
`DEFAULT_MAX_SEGMENT_RECORDS = 13_000` does NOT need retuning downward for a
12-market universe. It needs to be understood as universe-size-dependent.**

**Which bound is load-bearing has inverted.** In DEMO the record bound was
unreachable and `DEFAULT_MAX_SEGMENT_AGE_S = 900 s` was the only thing that ever
rotated. In production the record bound fires every ~93 s and **the age bound
never fired at all**. `DEFAULT_MAX_SEGMENT_BYTES = 32 MiB` was ~15× away
(~2.1 MB/segment) and is not load-bearing either.

**Headroom against L16.** The closer keeps up below roughly 6,900 events/s.
Measured peak is 485/s at 12 markets — **~14× of margin at this universe size**.
Under naive linear scaling in market count, the peak would reach that ceiling at
roughly **170 markets**. Stated with its caveat: frame rate is not linear in
market count, and §5.3 shows per-market rates vary by four orders of magnitude,
so 170 is an order-of-magnitude marker and not a capacity limit.

**Retention, L15's missing input.** ~13.8 MB compressed per 600 s at 12 markets
= **~83 MB/hour, ~2.0 GB/day**. L15 said a retention rule must be sized *from*
P4's first hour rather than guessed before it. This is that number, for this
universe size. It is still unowned.

### 5.3 Collector lag — stated precisely, not as a number it did not measure

This run emitted no `kalshi-live-tape.jsonl` interval record, so
`reader_lag_frames_max`, `append_us_max` and `segment_close_ms_max` were **not
captured**. Rather than report a lag figure, what was measured is:

* `frames_received == frames_yielded` (84,170 = 84,170) — nothing backed up out
  of the reader;
* `read_timeouts: 0`, `frames_malformed: 0`, `frames_oversize: 0`;
* `append_calls == events_received == records committed on disk` — append kept
  up with ingestion for the whole session, at 140/s mean and 485/s peak.

`reader_stall_ms_max: 580` is reported by the transport but **must not be read
as collector lag**: P3 §9.1 already types this field as misnamed and
mis-scoped, and its value here equals the maximum *interarrival* (580.91 ms) —
i.e. it measured a quiet venue, not a stalled reader.

### 5.4 Activity distribution, and a finding about our own selection statistic

Per-market frames over the session, against the manifest's frozen ranking:

| manifest rank | wire rank | stratum | traded c/min | frames |
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

**Spearman(traded contracts/min, wire frames) = 0.52** on n=12. Directionally
right in aggregate — high 58.3% of frames, medium 26.7%, low 15.0% — but with
severe individual inversions: **the market ranked last on trading rate produced
the 4th-most wire frames (11,885), and a medium-stratum market produced exactly
one frame in ten minutes.** The ranking statistic spans 1,868× while wire frames
span 19,741×.

This measures, rather than assumes, the caveat attempt 1 stated: *frame rate is
driven by quote updates as much as by trades.* The manifest declares this
limitation on its own face (`STATISTIC_LIMITATIONS`,
`stronger_alternative_not_used`); it is now quantified.

**`top_of_book_change_rate` is degenerate as a screen.** It read **1.00 for 11
of 12** markets and its Spearman against wire frames is **−0.20** — no
predictive value at this probe resolution (4 reads over 7.6 minutes reduces to
"did top-of-book move at least once", which is almost always yes). Doctrine 8,
applied to one of our own statistics.

**Consequence for `MARKET-MICROSTRUCTURE-EDGE-001`:** a universe stratified on
REST trading rate is not stratified on message rate. If that milestone wants
message-rate strata it must measure message rate, which requires a WebSocket
probe and not a REST census.

### 5.5 Did the DEMO frame-rate distribution predict production's? **NO.**

The REST census already answered the *trading*-rate half (no cliff; largest
adjacent step 2.53× vs DEMO's 98.3×). This is the wire half, and it is the
comparison the milestone was written around — **same collector, same three
channels, same 12-market universe size, same selection procedure**:

| | DEMO (L12) | **PRODUCTION (measured here)** |
|---|---|---|
| universe | frozen 12-market pool | frozen 12-market pool |
| frame rate | **0.75 frames/s** | **140.37 frames/s** |
| ratio | — | **~187× faster** |
| peak 1 s | not reached | **485 frames/s** |
| silent seconds | most of them | **0 of 600** |
| rotations | **0**, ever | **6** in 10 minutes |
| what rotates | `MAX_SEGMENT_AGE_S` (900 s) | `MAX_SEGMENT_RECORDS` (13,000) |
| 100,000-frame floor | 9.2× short over hours | would clear in **~12 minutes** |

**DEMO did not predict production on the wire either, and the gap is larger
than the REST comparison suggested.** DEMO's replication run produced 0.00
frames/s. Production produced a frame every 1.1 ms at the median and never went
quiet for a whole second.

The practical consequence: **every constant tuned against DEMO's rate was tuned
against a regime ~187× slower**, and the one that mattered most
(`DEFAULT_MAX_SEGMENT_RECORDS`) survives only because it was sized against an
*assumed* peak rather than against DEMO's *measured* rate. Had it been fitted to
DEMO, it would have been wrong by more than two orders of magnitude.

---

## 6. Replay equality — NOT QUALIFIED (B3)

Not computed, and it must not be. B3 is open: `archive.replay()` skips a
non-orderbook frame's `seq` and manufactures a gap that never happened. Capture
was authorized; the replay-equality verdict was not, and no B3 repair was made.

The tape is complete, digest-chained, `VALID` and re-replayable, so the verdict
can be computed over these same bytes after B3 is fixed. The artifact carries
`replay_equality: NOT_QUALIFIED:B3_OPEN`.

---

## 7. Quantities preserved as NOT MEASURABLE

Emitted as typed states, never as zeroes:

| quantity | state |
|---|---|
| `ticker_sequence_gaps` (sid **2**) | `NOT_MEASURABLE:empty_sequence_domain` |
| `ticker_completeness` | `NOT_MEASURABLE:no_loss_detector_exists` |
| `transport_dropped_frames` | `NOT_MEASURABLE:no_source_exists` |
| `recoveries_from_tape` | `NOT_RECONSTRUCTABLE_BY_DESIGN` |
| `generation_advances_on_unsequenced_sid` | `NOT_RECONSTRUCTABLE_BY_DESIGN` |
| `replay_equality` | `NOT_QUALIFIED:B3_OPEN` |

**`sequence_gaps = 0` on ticker is an empty domain, not an observation**, and
that is how production reported it. The same applies to `missing_seq = 0` on
sid 2: dispatch passes over frames carrying no `seq` at all, so the counter was
never reachable — it is an artefact of the same empty domain.

`venue_to_receive_offset_contaminated_ms` (n=84,154; p50 45.03 ms, p90 47.93,
p95 59.62, p99 511.61) is reported under that name and **is not a latency** —
the host clock offset is uncharacterised (`host_clock_offset_characterised:
false`).

---

## 8. Secondary findings — collector-side, environment-independent, NOT fixed

None is a production semantic discrepancy; none blocks any verdict above. All
are pre-existing on `main` and would read identically on DEMO. Recorded so each
is a one-line change with a reason attached rather than a rediscovery.

**8.1 `CollectorResult.subscription_generations = 3` in a session that had one
subscription generation.** `subscription_epoch_final` is 1, `metrics.
subscription_generation` is 1, `reconnects` and `disconnects` are 0, and every
tape record is stamped generation 1 — all correct. But the field increments once
per **router created** (`collector.py:1218`) *and* once per **router superseded**
(`collector.py:1241`); three sids were seen, so it reads 3. A P4 reader would
take that as "the subscription was re-established twice", which is false and
contradicts every other counter in the same object. The tape is unaffected; this
is a readout name that overstates its quantity.

**8.2 `CollectorResult.segments_committed = 1` in a session that committed 7.**
It is `len(self._archive.close())` — the segments committed by the *final* close
only (`collector.py:1709`). The six rotation-committed segments are counted in
`metrics.segments_closed = 7`, which is right. Same family as 8.1, lower
severity.

**8.3 `SubscriptionState.healthy = False` on the ticker and trade sids.** Both
report `state_reason: awaiting_snapshot` while functioning perfectly — sid 3
accepted 2,516 trades with zero faults. The health model presumes a snapshot
base, which only an orderbook subscription ever receives; `carries_orderbook:
false` distinguishes them. This is a false alarm rather than a false clean, so
it is the safe direction — but `healthy` cannot be used as a session-level gate
across mixed channels, and nothing currently says so.

**8.4 The B4 session claim is not written where its own docstring says.**
`session_root.py` publishes the claim at `ROOT/production/` while the archive
writes to `ROOT/env=production/` (`archive.py:448`). The claim therefore sits in
an empty sibling directory, not "beside the genesis". **The guarantee still
holds** — `session_claim_path()` is deterministic and per-environment, so a
second session against the same root still collides and is refused, and a
demo/production split still gets separate claims. Only the stated rationale
("the environment directory is the unit the B4 boundary actually lives in") is
inaccurate about which directory that is.

---

## 9. What could not be determined

* **Whether an `error` frame consumes a `seq` on the orderbook sid** (L8) —
  production sent zero error frames. Still open, on the conservative assumption.
* **Whether `EMPTY` (a transmitted but empty ladder) ever occurs** (L4) — 0 in
  13 production snapshots. n is far too small to retire the distinction.
* **Whether a venue-initiated disconnect behaves like a forced one** (L7) — 0
  disconnects in 600 s. Not exercised.
* **The delta-refusal path** `rejected_pre_generation_snapshot` (L5) — still
  never exercised live; production delivered every snapshot before its deltas.
* **Replay equality** — B3.
* **Any hour but this one.** 00:36–00:46 UTC on a night with MLB, ATP, MLS and
  WNBA in play. The manifest selected a sports-dominated universe because that
  is what was liquid at 20:36 ET. Rates at other hours, and for non-sports
  series, are unmeasured.
* **Scaling in market count.** Measured at 12 markets only.
* **Host clock offset**, hence any true latency.

---

## 10. Validation

| | |
|---|---|
| `git diff main -- app/ tests/` | **empty** — the collector ran exactly as shipped; this branch adds evidence and this document only |
| kalshi selection | **1,819 passed, 5 skipped, 4 xfailed, 0 failed** (378 s) |
| `tests/test_kalshi_tape_manifest_001.py` | **60 passed / 0 failed** — the fixture time bomb defused at `b0db246` stays defused; attempt 1's two failures do not recur |
| `tests/test_kalshi_prod_qual_precapture_001.py` | **46 passed** |
| structural order-API guard | **CLEAN** — 16 modules, 3,292 identifiers, 0 findings, on the throwaway clone |
| safety grep (AGENTS.md) | **clean** — every hit is a boundary-statement docstring; no implementation surface |
| credential | never copied, printed or logged. Only a key-id fingerprint (`sha256:cfdd78afeded1c22`) and a path basename appear in any artifact; `key_material_read_by_this_script: false` |

### Boundary

`OBSERVE_ONLY` throughout. **One** outbound command reached the venue — the
`subscribe` for three market-data channels — with `commands_refused: 0` and no
`get_snapshot` required. No order, cancel, portfolio mutation, private
order/fill channel, capital or execution-module dependency was involved.

### EVO

The live Solana lane (`watch-loop`, 15 days uptime) was untouched and is still
running. Every process this run started has exited. The throwaway clone at
`/tmp/kalshi-p4-attempt2` — which held a copy of `.env` — was removed. EVO's own
checkout is on `main` at `b0db246` with a clean working tree. The tape (14 MB)
is retained as evidence at
`~/kalshi-prod-tape/p4-attempt2-20260820T003519Z`, session
`s-20260820T003520Z-f450f75ed1fc`.

### Not done, deliberately

No B3 repair. No alpha, features or model work. No collector-semantics change to
accommodate production — none was needed. `kalshi.py:52-55` not edited despite
B1 now being closable. The four secondary findings in §8 not fixed. Nothing
merged; `main` untouched.
