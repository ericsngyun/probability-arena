# KALSHI-CP7-LIVE-RERUN-001

**Status: DELIVERED on branch `KALSHI-CP7-LIVE-RERUN`, NOT MERGED.** Run
2026-08-17, after `KALSHI-REPLAY-GENERATION-CONSISTENCY-001` merged. All three
preregistered CP7 properties proved **live**; one further behaviour the fix
added was **not exercised** and is reported as such.

Authorizes no capital, no orders, no live trading behaviour. Read-only market
data only. Nothing was deployed.

---

## Why this run exists

CP7 FAILED on the live venue: at both forced generation boundaries the first
snapshot republished **all 60 markets at once**, 59 of them still carrying
ladders from the epoch the venue had abandoned. The fix is merged and was
proved on the venue's own captured frames — but **offline is not a live
qualification**, and the agent that wrote the fix explicitly declined to claim
CP7 on it. This is the live claim, earned separately.

## Scope, stated before anything else

**60 VENUE TEST INSTRUMENTS** (`KXMAXSHARDINGTEST`, `KXTESTMATCH`). DEMO's
ordinary markets are nearly silent — 98.3% of the frames its eligible
population emits come from 194 venue test instruments — so these are used
**deliberately**: a functional proof needs frames that exercise the code paths,
not frames that resemble production activity.

**This is a FUNCTIONAL PROOF ONLY.** Per §8 of the qualification
preregistration, **no rate, latency-tail, throughput, capacity or
microstructure-realism claim** may be derived from anything here, and no frame
count below should be read as one. Every artifact carries that sentence in its
own `scope_note`.

---

## The universe, frozen before any socket existed

`scripts/kalshi_cp7_live_rerun_freeze_universe.py`, one public credential-free
`GET /trade-api/v2/markets`, **frozen at 2026-08-17T22:03:58Z** — before the
first session started at 22:05. The rule was written in the module docstring
before the query ran:

* continuity first — the 2026-08-17 CP7 session's tickers that are still open,
  in their original order. **57 of 60** survived; the three
  `KXTESTMATCH-26AUG122030BANAUS-*` legs had closed.
* the 3-market shortfall topped up **telemetry-blind**, by ticker ascending.
  The script reads no volume, rate, liquidity or top-of-book field **and does
  not record one**, so a market cannot enter this universe because it looks
  chatty and no later amendment can make it possible. Preregistration §1: *no
  ticker may be replaced because its telemetry looks cleaner.*
* the full candidate population and the freeze timestamp are written beside the
  selection, so the sampling frame is explicit.

## The probe was improved first, and committed first

CP7's 2026-08-17 artifacts recorded a bare `publishable` boolean, which is why
the failure they contained had to be **reconstructed** from the frame stream
afterwards. Doctrine 10 applies to the artifact as much as to the code — three
different conditions produce that `false`, and they are not the same
observation.

* `_book_state` now records `publication_state`, `publication_reason` and
  **both** epoch numbers, plus `based_generation` and
  `based_for_current_generation` — the invariant's two inputs, not only its
  verdict.
* the transition log detects on the **typed** tuple, so a book moving from
  `awaiting_snapshot_for_generation` to `book_halted` is visible. `from`/`to`
  keep their former boolean meaning; `from_state`/`to_state` are new.
* a new `generation_delta_refusals` lane. A refusal changes no publication
  state, so the transition log cannot see it, and *"did not happen"* and
  *"happened invisibly"* would otherwise be indistinguishable.

## The three sessions

Read-only, from a throwaway clone on EVO. EVO's production checkout stayed
clean at `6af41ca` on `main` before and after, the clone was deleted, and every
process was killed. The live Solana lane was not touched.

| session | duration | perturbation | frames | archived | faults | recoveries | epoch |
|---|---:|---|---:|---:|---:|---:|---:|
| `s1-observe-rerun` | 180 s | **none** (the negative control) | 8,983 | 8,983 | **0** | 0 | 1 |
| `s2-reconnect-rerun` | 180 s | 2 forced socket teardowns | 9,628 | 9,628 | 0 | 0 | **3** |
| `s3-drop-rerun` | 120 s | 1 withheld orderbook frame | 6,332 | 6,332 | **9** | **1** | 1 |

Zero rejected, zero malformed, zero metrics errors in all three.

---

## Property 1 — `generation_after > generation_before`. PROVEN.

Two teardowns of the **real** socket, so the collector's own read raises its
own `TransportError` and walks its own reconnect ladder. Epoch observed
advancing **1 → 2 → 3** across the timeline — boundary by boundary, not merely
as a final counter — matching two journal entries and the metrics lane's
`reconnects = 2`, `disconnects = 2`.

**The paired control:** `s1-observe-rerun`, same universe, no forced close —
epoch **1**, reconnects **0**. The counter is not pinned high.

## Property 2 — per-market independent re-acquisition. PROVEN. *(the one CP7 failed)*

Asserted on the **shape of the transition log**, never on an aggregate — the
aggregate is what hid this defect for a whole qualification session.

```
frame    4 …  63   epoch 1   60 SEPARATE entries, one per market
frame  601        epoch 2   subscribed ack → 60 markets → subscription_unhealthy
frame  604        epoch 2   orderbook_snapshot seq=1 for T53599.99
                            →  1 market  publishable                  (that market)
                            → 59 markets awaiting_snapshot_for_generation
frame  605 … 663  epoch 2   59 SEPARATE entries, one acquisition each
frame 1201/1204   epoch 3   the same shape again
```

**Frame 604 is the exact frame that republished all 60 markets in the original
session. It now republishes one** — the market whose own snapshot it is.

| | epoch 2 | epoch 3 |
|---|---:|---:|
| markets unpublished at the boundary | **60** | **60** |
| acquisitions | 60 | 60 |
| **entries carrying an acquisition** | **60** | **60** |
| **max acquisitions in any one entry** | **1** | **1** |
| markets left `awaiting_snapshot_for_generation` at the first new snapshot | 59 | 59 |

The first row is the preregistration's other clause — *no book may silently
survive across a generation boundary as if nothing happened* — checked
directly: between the last acquisition of the previous epoch and the first of
this one, **all 60** markets were taken out of publishable state (59 last seen
as `awaiting_snapshot_for_generation`, and the one that re-acquired on the
rebasing frame itself last seen as `subscription_unhealthy`).

Every acquisition was caused by an `orderbook_snapshot` **naming that same
market**; every market acquired exactly once; and each of the 59 left-behind
books carried the **abandoned** epoch (`based_generation == epoch - 1`) rather
than being silently carried over. That last check is the one that would have
caught the original defect on its own.

## Property 3 — a within-generation gap still faults. PROVEN, and typed.

The anti-vacuity control, and the one that mattered most here: the fix added a
**benign** typed state, and a fix that lets real faults land in a benign state
is indistinguishable from a fix that broke the detector.

One `orderbook_delta`, sid 1, **seq 301**, market `…T69299.99`, withheld from
the collector **inside generation 1**.

| | observed |
|---|---|
| metrics `sequence_gaps` | 0 → **1** |
| session `sequence_faults` | **9** |
| recoveries requested | **1** — one command for one hole |
| the halt was caused by | the frame at **seq 302**, i.e. the one after the hole |
| books unpublished | **all 60**, in one step |
| **their typed state** | **`book_halted`** — all 60 |
| books filed under `awaiting_snapshot_for_generation` anywhere in this session | **0** |
| books re-acquired afterwards | **60**, each on its OWN snapshot, one per entry, inside one generation |

**The benign state did not absorb the fault.** In a session with no generation
boundary at all, not one book was reported under the boundary reason.

**The paired control:** `s1-observe-rerun` — 0 faults, and not one halted book
in its entire timeline.

**One number differs from the original session and should not be read as a
regression.** `sequence_faults` is **9** here against **14** in the original
`s3-drop`. The decomposition was measured rather than assumed: the counter
stands at **1** at the halt (frame 372) and at **9** by the first re-acquisition
(frame 381), so it is **1 gap + 8 deltas refused while the subscription awaited
its new base**. The second term is how many deltas the venue happened to send
before the recovery snapshot landed — a venue timing quantity, not a detector
quantity. The **detector** numbers are identical across both runs:
`sequence_gaps` 1, `sequence_regressions` 0, `sequence_duplicates` 0,
recoveries 1, all 60 books unpublished, halt caused by the frame immediately
after the hole.

**And none of those 9 was a generation refusal**, which matters because a
refusal also lands in `sequence_faults` (see below):
`rejected_pre_generation_snapshot` is **0** on every book in this session, so
the fault count here is entirely the withheld frame and its aftermath.

## The delta-refusal path — **NOT EXERCISED**. Reported, not claimed.

The fix went further than the invariant required: a new-generation delta
landing on an un-re-snapshotted book is **refused**
(`rejected_pre_generation_snapshot`) rather than applied to an abandoned
ladder. CP7 could only report that this *"did not happen to occur"*.

**It did not occur again.** Zero refusals observed, zero counted on any book.
No new-generation delta arrived for any market before that market's own
new-generation snapshot, across a **59-frame re-snapshot window** at each
boundary. The venue's ordering was favourable a second time.

**That is a fact about this session's frame ordering, not evidence about the
guard, and it is not presented as a pass.** Two things make the zero worth
more than the original run's:

1. it is now a **measurement** — the probe has a lane for refusals, so the
   absence is observed rather than inferred from the defect's absence;
2. the observer is proved to fire by a **forced positive control**
   (`TestTheGenerationDeltaRefusalIsObservable`): a stream where a
   new-generation delta lands on an un-re-snapshotted book produces a recorded
   refusal naming that market, and the same boundary **with** the snapshot
   refuses nothing and applies the delta. So an empty list means *"did not
   fire"*, not *"nothing was watching"*.

To exercise it live we would need the venue to interleave a delta into the
re-snapshot window, which we cannot cause without perturbing frame order — and
a tap that reordered frames would be measuring the tap.

**A trap this milestone pins, because it will otherwise be misread once the
guard does fire.** A refusal reaches the collector as a `BookIntegrityError`
and is counted by the generic book-level handler, so **`sequence_faults` moves
even though no message was lost**. A future reconnect session with a non-zero
fault count is therefore not necessarily a faulting one. The discriminator is
`rejected_pre_generation_snapshot`, which the probe now records per book and
per event, and the pairing is asserted in the positive control.

---

## The verifier can fail, and is proved on the real defect

`scripts/kalshi_cp7_live_rerun_verify.py` is offline and pure; it recomputes
the verdict from the committed artifacts, so the claim is re-runnable rather
than a paragraph someone wrote after reading a log.

Its positive control is **not synthetic**. Pointed at this repository's own
recording of the failure —
`docs/experiments/KALSHI-CP6-CP9-FUNCTIONAL-RUNS/s2-reconnect-session.json` —
`property_2` **fails**, and fails on the **CP7 shape** (`one frame republished
60 markets at once`) rather than on a missing field. The same artifact is
independently confirmed to contain the defect: one entry, 60 acquisitions, 59
of them caused by a **sibling's** snapshot.

The CP7 shape guard in
`tests/test_kalshi_replay_generation_consistency_001.py` was adjusted to count
**acquisitions** per entry rather than every typed change, because the richer
transition log means the first snapshot of a generation legitimately changes
all 60 books. That this did **not** disarm it was measured, not assumed: with
the invariant reverted an entry carries **11** acquisitions and the assertion
still fires; with the fix present every entry carries exactly **1**.

## Safety — audited from the sessions' own wire record

**Six commands reached a socket in total**, across all three sessions, which is
every byte this milestone sent to the venue:

| session | commands |
|---|---|
| `s1-observe-rerun` | 1 × `subscribe` |
| `s2-reconnect-rerun` | 3 × `subscribe` (one per connection) |
| `s3-drop-rerun` | 1 × `subscribe`, 1 × `update_subscription` (the recovery) |

Channels used, across all six: `orderbook_delta`, `ticker`, `trade` — nothing
else, all inside `kalshi.ALLOWED_CHANNELS` and refused at `CollectorConfig`
construction otherwise. **No order, position, portfolio, wallet or
key-management surface was reached**, and no private channel was subscribed. No
key material was printed: the credential audit records a key-id fingerprint and
nothing else. The universe freeze touched no socket at all: it used the
**unauthenticated** public `GET /markets` route, queried once per test series.

EVO: the throwaway clone was deleted, every process was killed, and the
production checkout was verified clean at `6af41ca` on `main` afterwards. The
clone read the credential through a **symlink** to the production `.env`, so no
second copy of key material was ever written to disk.

## Suite state, and the attribution of every failure

* `tests/test_kalshi_cp7_live_rerun_001.py` — **12 passed** (new).
* `tests/test_kalshi_cp6_cp9_functional_001.py` — **26 passed** (22 → 26).
* `tests/test_kalshi_replay_generation_consistency_001.py` — **25 passed**.
* The `realtime / kalshi / collector / archive / replay` keyword selection, run
  together: **1,768 passed, 5 skipped, 4 xfailed, 0 failed** (3,424 deselected).
* Full suite, `pytest -q -p no:randomly`, loaded host: **5,195 passed, 8
  failed, 6 skipped, 4 xfailed** in 16 m 22 s, against the recorded
  **5,171 / 17** baseline.

Passes rise by 24: **16 are this milestone's** (12 new in
`test_kalshi_cp7_live_rerun_001.py`, 4 added to
`test_kalshi_cp6_cp9_functional_001.py`) and the rest are members of the
wall-clock class that happened to pass this time.

**All 8 failures are the known wall-clock/staleness class**, in the files that
class already owns — `test_live_market_001` (4), `test_marketops`,
`test_ops009`, `test_tennis_candidate_order_001`, `test_tennis_live_source_001`.
The count is *lower* than the baseline's 16 because **the failing membership of
that class rotates under load**; it is not a number this branch improved, and it
is not read as one. The visible assertion is literally a clock:
`assert 1152.6 <= 900` on a quote age, in a suite that took 16 minutes.

Attributed by **measurement**, the three checks the CP9 report requires:

1. **All 8 pass in isolation** — those five files together: **113 passed in
   9.3 s**, green.
2. **None of those five files imports `app.realtime`** at all (grep returns
   nothing), and this branch's diff against `app/` is **empty** — it changes no
   production code whatsoever.
3. The `realtime / kalshi / collector / archive / replay` selection, run
   together: **1,768 passed, 0 failed**.

Safety grep (`AGENTS.md`) over `app/`: **clean and unchanged** — necessarily, on
an empty `app/` diff. The single hit across this milestone's own files is a
pre-existing docstring boundary statement in the probe ("No order, position,
portfolio, wallet or key-management surface is reached"), which is the declared
acceptable form.

---

## What this does and does not clear

**Clears:** `Reconnect behavior` in the CP9 verdict, on live evidence.
`MARKET-MICROSTRUCTURE-EDGE-001`'s reconnect blocker is closed in both lanes —
a stale pre-reconnect ladder can no longer be presented as current, and that is
now proved on the venue rather than on a recording of it.

**Does not clear:** anything else. The four lower lines of the CP9 verdict —
DEMO throughput, production latency, production capacity, microstructure
realism — are exactly as unestablished as before, and this document contains no
evidence bearing on them. Nor does it establish that a **venue-initiated**
disconnect behaves like a forced one, or that `get_snapshot` repairs a loss the
venue actually suffered; both frames were removed by us.

**Open, and narrower than before:** the delta-refusal path has never been
exercised on the venue, in either run.

**One thing that cannot be gone back for.** CP8 was **not** re-run — it was
already QUALIFIED and this milestone is scoped to CP7 — and the three tapes
were deleted with the throwaway clone, so these particular sessions can never
be replayed. What survives is the session JSON: the transition log, the typed
per-market terminal state, the wire census and the command audit, which is
everything CP7's three properties are computed from. A future conservation or
replay claim needs its own capture; it must not be back-derived from these
artifacts.
