# MARKET-MICROSTRUCTURE-EDGE-001 — confirmation tranche ledger

**BLIND CAPTURE IN PROGRESS.** 20 sessions, ≥14 days, `dataset_role=CONFIRMATION`.

Authorised 2026-08-24 after: sampling-runner qualification (12/12 invariants),
row-builder qualification (11/11 mutations), the row-v2/label-v2 seam smoke
test, Amendment 3 (`realized_vol_1s` removed) and Amendment 4 (eligibility
gates on session-remaining, five bins proven reachable).

## What may and may not be inspected during capture

**Permitted:** capture health · sequence/archive integrity · safety peak ·
row/schema validity · mechanical power accumulation · session and TTE-bin
coverage · series and weekend coverage.

**Forbidden:** returns by feature · M0/M1 losses · coefficients · feature
importance · OFI/markout relationships · flow-conditioned returns · which
sessions "look promising."

The only adaptive action permitted is **scheduling future sessions to satisfy
already-preregistered coverage constraints** — never to improve a result.

## Bin allocation — 4/4/4/4/4, hardest first

| order | primary bin | sessions | status |
|---:|---|---:|---|
| 1 | `late_resolution` | 4 | **4 of 4 — quota met** |
| 2 | `live_event` | 4 | **1 of 4 complete** |
| 3 | `near_event` | 4 | not started |
| 4 | `approaching` | 4 | not started |
| 5 | `far` | 4 | not started |

A session counts toward bin *b* only if **a complete 300 s post-warmup panel
interval lies wholly within b**.

## Sessions

### Scheduling record (per session, frozen BEFORE capture)

`scheduled_target_bin` · `anchor_event_id` · `anchor_occurrence_datetime` ·
`scheduled_session_start` · `selection_rule_version` ·
`candidate_count_at_freeze` · `replacement_reason`

Timing and identity only. No activity outcomes, prices, returns or feature
statistics — enforced by an AST guard over
`scripts/kalshi_microstructure_schedule_anchor.py`, which fails if the
scheduler so much as references a volume or price field.

### Hard-bin ordering

`S02 live_event → S03 late_resolution → S04 live_event`, until both difficult
strata hold ≥3 qualifying sessions. Then `near_event`, `approaching` and `far`
fill out the 4/4/4/4/4 allocation.

| # | label | primary bin | start (UTC) | length | subscribed | status |
|---:|---|---|---|---:|---:|---|
| 01 | `MMEDGE-S01-late_resolution-20260824` | `late_resolution` | 2026-08-24T00:41:58Z | 10,800 s | 24 | **CLEAN — counts** |
| 02 | `MMEDGE-S02-live_event-20260824` | `live_event` | 2026-08-24T16:40:00Z | 10,800 s | 24 | **CLEAN — counts** |
| 03 | `MMEDGE-S03-late_resolution-20260825` | `late_resolution` | 2026-08-25T01:45:04Z | 10,800 s | 8 | **CLEAN — counts** |
| 04 | `MMEDGE-S04-late_resolution-20260826` | `late_resolution` | 2026-08-26T01:45:02Z | 10,800 s | 24 | **CLEAN BUT EMPTY — does not count, S21+** |
| 05 | `MMEDGE-S05-late_resolution-20260826` | `late_resolution` | 2026-08-26T21:05:01Z | 10,800 s | 24 | **CLEAN — counts** |
| 06 | `MMEDGE-S06-late_resolution-20260827` | `late_resolution` | 2026-08-27T18:05:03Z | 10,800 s | 24 | **CLEAN — counts** |

### Session 01 — pre-capture record

Candidate set frozen before capture by the Addendum 1 rule
`(occurrence_datetime ASC, ticker ASC)` over open markets in the eligible
series whose event lies in the assigned bin.

* 24 open markets, all `KXWTAMATCH` (16) and `KXATPMATCH` (8)
* events **5.69–6.69 h in the past** — genuinely `late_resolution`, i.e. past
  the nominal occurrence time and **not** settled
* exactly the concurrency ceiling, so no truncation was required
* preflight: `40,000,000 > 3,500 × 10,800 = 37,800,000` ✓

**This session is also the feasibility discovery** Amendment 4 made possible.
Whether books past their nominal event time still publish enough sequenced
activity to clear the 30-events-per-300 s floor is **not established by any
tape we hold** — every prior session was 2–3 h pre-event. If the panel comes
back persistently empty, `late_resolution` is thin at this venue, and that is a
reportable finding rather than a reason to substitute an easier bin.

### Session 01 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 capture validity — PASS.** Terminal `capped_time`.
`events_received == events_archived == 1,019,783`. `frames_malformed`,
`events_rejected`, `rotation_failures`, `sequence_faults`, `reconnects` all
**0**. 81 segments committed. `peak_1s_sliding` **1,210** against the 3,500
stop.

**L2 sampling validity — PASS.** First tick exactly at open+300 s; no warmup
tick emitted a panel; tick gaps exactly `[300.0]`; max panel size **12**, K
respected; reason vocabulary closed with no unexpected reasons.

The final two ticks returned **empty panels, correctly**: at open+10,500 s and
open+10,800 s the scheduled session-remaining was 600 s and 300 s, and the
Amendment 4 gate is *strictly* greater than 600 s. The gate is visible working
in production.

**L3 dataset validity — PASS.** `microstructure-row-v2` / `label-v2`;
13 M0 and 17 M1 columns; minimum completeness **0.978 / 0.9781**; **no
always-missing columns**; `dataset_role=CONFIRMATION`; **113,100 rows**;
0 dispatch errors.

Label coverage: **0.9780 / 0.9779 / 0.9770 / 0.9674** at 1/5/30/300 s. The 300 s
horizon holds up at 96.7%, as the 3 h projection expected.

**L4 coverage outcome — the feasibility question is answered.**

| | |
|---|---|
| covering intervals wholly in `late_resolution` | **377** |
| counts toward target bin | **yes** |
| markets clearing the 30-events/300 s floor | **24 of 24** |
| markets ever selected | 21 |
| market-blocks accumulated | **377** |
| market/session clusters | **21** |
| bins touched | `late_resolution` only |
| no-row reasons | `eligible_but_never_top_K`: 3 |

**Post-event, unsettled books are dense enough to support the panel.** Every
one of the 24 markets cleared the activity floor. **No market closed or
resolved mid-session, and none failed activity eligibility** — the only reason
any market went unsampled was losing a top-12 competition, which is the
mechanism working, not a venue limitation.

That is a real finding about Kalshi's lifecycle: `TTE < 0` is not `market no
longer observable`, which is exactly the distinction Amendment 4 was written to
preserve. Under the old `TTE > 600 s` gate this session was impossible.

**Power, measured rather than projected.** 377 market-blocks and 21 clusters
from one session, against projections of 420 and a conservative 12. At this
rate 20 sessions yield ≈7,540 blocks (floor 4,000) and well past 150 clusters.

### S02 — deferred to a fuller slate, deliberately

Not launched on the night of 2026-08-23. The frozen design schedules around
event timing, so there is no reason to spend a `live_event` session on a
thinning late-Sunday card. **S01 already established that the hard strata are
available**, so nothing is at risk in waiting: all 24 post-event markets
cleared the activity floor, and the only exclusions were legitimate top-K
competition.

The anchor is chosen by `kalshi_microstructure_schedule_anchor.py`, not by
hand. It enumerates open markets in the eligible series, computes the required
start per bin, and takes the **earliest feasible occurrence time** — it cannot
prefer a busier-looking game because it never sees activity.

### Anchor arithmetic, per bin (3 h session)

| target bin | session start | complete intervals inside the bin |
|---|---|---:|
| `far` | event − 545 min | 35 |
| `approaching` | event − 365 min | 35 |
| `near_event` | event − 125 min | 20 |
| **`live_event`** | **event − 20 min** | **3** |
| `late_resolution` | event + 5 min | 35 |

`event − 20 min` puts the first post-warmup tick at TTE ≈ 900 s, the top of the
stratum.

**`live_event` is structurally the thinnest stratum**, and always will be: the
bin is 900 s wide, so a session can contribute at most three complete 300 s
intervals no matter how long it runs. Four sessions therefore yield ~12
covering intervals against ~140 for each of the wide bins. That is a
consequence of the frozen bin boundaries, not a defect, and it is **not** a
reason to widen them after the fact — but per-bin block counts will be sharply
unbalanced and the analysis must not read that imbalance as a property of the
markets.

---

## A note for the future evaluator: unequal block counts are geometric

Per-bin block counts will be **sharply unbalanced**, and the cause is the
frozen bin widths, not the markets:

| bin | TTE width | time-intervals / session | market-blocks / session (K=12) | over 4 sessions |
|---|---:|---:|---:|---:|
| `far` | unbounded | 35 | 420 | ~1,680 |
| `approaching` | 14,400 s | 35 | 420 | ~1,680 |
| `near_event` | 6,300 s | 20 | 240 | ~960 |
| **`live_event`** | **900 s** | **3** — `900/300` | **36** | **~144** |
| `late_resolution` | unbounded | 35 | 420 | ~1,680 |

**Correction (2026-08-23):** an earlier revision of this note said four
`live_event` sessions yield "~12 blocks against ~140". That conflated
*time-intervals* with *market-blocks* — each qualifying interval yields up to
K=12 market-blocks. The corrected figures are **~144 against ~1,680**. The
**ratio** was right (~12×); the absolute counts were an order of magnitude low.
S01 bears the scale out: 377 market-blocks in one `late_resolution` session,
90% of the 420 ceiling. **That is geometry, not evidence.** It
says nothing about whether live-event markets are sparse, quiet or
uninformative — S01 already showed the analogous post-event stratum was dense,
with 24 of 24 markets clearing the activity floor.

The hazard is pooling: weighting every block equally and reading the result as
"the experiment's average regime" lets the wide bins dominate purely because
they contain more clock time.

**No weights are changed here.** This is documentation so the imbalance is
never mistaken for market behaviour.

### ⚠ The TTE-stratified view is NOT currently a preregistered cell

Checked rather than assumed, and it matters:

* **§7 freezes exactly twelve cells** — 3 comparisons × 4 horizons —
  Benjamini–Hochberg at FDR 10% "computed once, on the pre-declared set", and
  states plainly: *"No cell may be added after seeing results; a cell that
  looks interesting later starts a new preregistration with a new name."*
* **Amendment 2 §F** names **series** as strata/evaluation groups/covariates
  and clusters uncertainty at event/market level. It does not name TTE.
* **Amendment 2 §C** froze the TTE bins and observes they *allow* asking
  `E[r(t+h) | OFI(t), TTE(t)]` later — but never registers that as a cell.

So "M0 vs M1 by TTE stratum" is currently **an unregistered analysis**. Running
it after the tranche would violate §7; registering it now is legitimate but
multiplies the cell count (12 × 5 = 60) and materially changes the FDR
correction.

**This needs a decision before the tranche completes, not at analysis time** —
by then the only honest options are to skip the stratified view entirely or to
start a separate preregistration under a new name. Recorded here so the choice
is made with a clear head and no results in view.

### Session 02 — scheduling record, frozen before capture

Chosen by `kalshi_microstructure_schedule_anchor.py`, not by hand.

| field | value |
|---|---|
| `scheduled_target_bin` | `live_event` |
| `anchor_occurrence_datetime` | `2026-08-24T17:00:00Z` |
| `scheduled_session_start` | `2026-08-24T16:40:00Z` (occurrence − 20 min) |
| `session_seconds` | 10,800 |
| `selection_rule_version` | `capture-plan-addendum-1+2 / edge-amendment-4` |
| `candidate_count_at_freeze` | 24 (all 24 on the anchor slate) |
| `pool_size_at_freeze` | 509 markets, 23 distinct occurrence times |
| `feasible_anchors` | 23 — the earliest was taken |
| `projected_covering_intervals` | 3 (the `live_event` maximum) |
| `replacement_reason` | none |
| `frozen_at_utc` | `2026-08-24T15:55:46Z` |
| scheduler commit | `73a7017` |

The winner is `KXATPMATCH` — 23 anchors were feasible and the rule took the
earliest, with no reference to activity, price or any S01-derived information.
Launch is armed by a timer that waits for the frozen start and then runs the
session unchanged, so the start time cannot drift by however long a human takes
to notice the clock.

### ⚠ Series concentration is accumulating on the hard bins

| session | bin | series |
|---|---|---|
| S01 | `late_resolution` | `KXWTAMATCH` ×16, `KXATPMATCH` ×8 — tennis |
| S02 | `live_event` | `KXATPMATCH` ×24 — tennis |

Both hard-bin sessions are tennis, and that is not coincidence: the hard bins
need anchors whose *timing* qualifies, and tennis has many more distinct
occurrence times per day than baseball. The deterministic
earliest-feasible-anchor rule will therefore keep selecting tennis for
`live_event` and `late_resolution`.

This pulls against the frozen spread constraints — **≥6 of 8 series**, **no
series more than 4 primary sessions**, and **each bin across more than one
series where feasible**. If S03 and S04 also land on tennis, the tennis budget
is spent on two strata and the wide bins must carry all remaining series
diversity.

**Nothing is changed on this basis now.** Adjusting the scheduler to prefer a
different series would make it activity-adjacent and defeat the AST guard.
Recorded so the tension is visible while it is still cheap to address inside
the frozen rules — e.g. by taking the earliest feasible anchor *of the target
bin* on a day whose slate is not tennis-dominated, which is a scheduling input,
not a selection signal.

### Session 02 — verdict: **OPERATIONALLY CLEAN**, counts toward `live_event`

**L1 capture validity — PASS.** Terminal `capped_time`.
`events_received == events_archived == 1,203,459`. `frames_malformed`,
`events_rejected`, `rotation_failures`, `sequence_faults`, `reconnects`,
`recoveries_requested` all **0**. 94 segments committed. `peak_1s_sliding`
**1,328** against the 3,500 stop.

**L2 sampling validity — PASS.** First tick exactly at open+300 s; no warmup
tick emitted a panel; tick gaps exactly `[300.0]`; max panel **12**, K
respected; reason vocabulary closed, no unexpected reasons; no ticker or volume
in eligibility. The final two ticks returned empty panels, correctly — the
Amendment 4 session-remaining gate again.

**L3 dataset validity — PASS.** `microstructure-row-v2` / `label-v2`; 13 M0 and
17 M1 columns; minimum completeness **0.996 / 0.996**; **no always-missing
columns**; `dataset_role=CONFIRMATION`; **118,800 rows**; 0 dispatch errors.

Label coverage **0.9960 / 0.9958 / 0.9951 / 0.9909** at 1/5/30/300 s — the 300 s
horizon at 99.1%, better than S01's 96.7%.

**L4 coverage outcome — earned under the frozen rule.**

| | |
|---|---|
| covering intervals wholly in `live_event` | **24** |
| counts toward target bin | **yes** |
| market-blocks accumulated | **396** |
| market/session clusters | **21** |
| markets ever eligible / selected | 23 / 21 of 24 |
| bins touched | `live_event`, `late_resolution` |
| no-row reasons | outranked ×2, **naturally closed/resolved ×1** |

The session earned `live_event` coverage on **24** covering intervals. The
question was never whether 396 blocks "look like enough" — it was whether a
complete 300 s post-warmup interval fell wholly inside the stratum, and 24 did.

**The closed-vs-quiet distinction produced its first real answer.**
`KXATPMATCH-26AUG24COPROD-COP` was **naturally closed or resolved mid-session**,
not quiet — a fact wire data alone cannot establish, and exactly why that field
exists. Zero markets stayed open and failed activity eligibility.

### Tranche ledger after S02

| bin | target | complete | remaining |
|---|---:|---:|---:|
| `late_resolution` | 4 | 1 | 3 |
| `live_event` | 4 | 1 | 3 |
| `near_event` | 4 | 0 | 4 |
| `approaching` | 4 | 0 | 4 |
| `far` | 4 | 0 | 4 |

| quantity | accumulated | floor | status |
|---|---:|---:|---|
| market-blocks | **773** (377 + 396) | 4,000 | 19% after 2 of 20 |
| market/session clusters | **42** (21 + 21) | 150 | 28% after 2 of 20 |

| series | primary sessions used | budget (≤4) |
|---|---:|---:|
| `KXWTAMATCH` | 1 (S01) | 3 remaining |
| `KXATPMATCH` | 1 (S02) | 3 remaining |
| other 6 series | 0 | — |

**Series represented: 2 of 8** (floor ≥6). **Weekend sessions: 1 of ≥4.** Both
strata so far are tennis, as flagged before S02.

**Correction (2026-08-24), derived from code rather than restated.** An earlier
revision of this line said *"Weekend sessions: 0 of ≥4 — S01 ran Sunday 00:41Z,
which is **Saturday** evening ET"*. Both halves were wrong: 00:41Z Monday minus
four hours is **Sunday 20:41 ET**, which **is** a weekend session. The frozen
rule (`SessionRecord.is_weekend_et`) classifies in ET precisely because a UTC
weekday would credit the wrong day, and the code has said `weekend_sessions: 1`
since the coverage scheduler was built — the prose was stale, not the rule.

| session | start (UTC) | start (ET) | weekend |
|---|---|---|---|
| S01 | `2026-08-24T00:41:58Z` | Sunday 2026-08-23 20:41 | **yes** |
| S02 | `2026-08-24T16:40:00Z` | Monday 2026-08-24 12:40 | no |

Authoritative coverage state, code-derived: **2 sessions · 1 weekend (3 still
required) · 2 of 8 series (4 still required) · 18 bin-sessions outstanding
across 18 remaining sessions.** All three quotas remain satisfiable, though the
bin quota now has **zero slack** — every remaining session must count toward
its target bin.

---

## Session 03 — scheduling decision (coverage layer → anchor layer)

**Not launched. Decision produced and frozen; start is 4.25 h out.**

### Coverage layer — which obligation, which slate

| | |
|---|---|
| sessions counted | 2 |
| bin remaining | `late_resolution` 3 · `live_event` 3 · `near_event` 4 · `approaching` 4 · `far` 4 |
| series used | `KXWTAMATCH` 1, `KXATPMATCH` 1 |
| weekend | 1 done, **3 still required** |
| **next obligation** | **`late_resolution`** (hard bins first) |

Feasible slates, by timing alone:

| day (ET) | series with a feasible `late_resolution` anchor |
|---|---|
| 2026-08-24 | ATP, MLBGAME, MLBHR, MLBTOTAL, WNBAGAME, WNBATOTAL |
| 2026-08-25 | + WTA |
| 2026-08-26 | ATP, MLBGAME, MLBTOTAL, WNBAGAME, WNBATOTAL, WTA |
| 2026-08-27 | MLBGAME, NFLGAME, WNBAGAME |
| 2026-08-28/29 | NFLGAME |

**Selected: `2026-08-24`, preferred series `KXMLBGAME`.**
Reason: *"KXMLBGAME is not yet represented (2/6); earliest qualifying date, ties
broken by day then series."*

The scheduler reached baseball **on its own, from the diversity obligation** —
not because anyone judged baseball more interesting. That is precisely the
degree of freedom this layer exists to remove.

### Anchor layer — which occurrence on that slate

| field | value |
|---|---|
| `anchor_occurrence_datetime` | `2026-08-25T01:40:00Z` |
| `scheduled_session_start` | `2026-08-25T01:45:00Z` (occurrence + 5 min) |
| `series_restriction` | `KXMLBGAME` (supplied by the coverage layer) |
| candidates at freeze | **8** (4 on the anchor slate) |
| feasible anchors considered | 3, earliest taken |
| projected covering intervals | **35** |

### ⚠ Diversity costs blocks, and that is a real trade

Restricting to `KXMLBGAME` yields **8 candidates**, not the 24 ceiling — that
series simply has fewer markets per day than tennis. With K = 12, the panel is
then bounded by **availability, not by K**: at most 8 markets, so roughly
8 × 35 ≈ **280 market-blocks** against S02's 396.

Nothing is changed on this basis. The coverage floors (`≥6 of 8 series`) and
the power floors (4,000 blocks, 150 clusters) are both frozen, and satisfying
one costs a little of the other. Recorded so that a later shortfall in blocks
is read as **the price of the diversity obligation**, not as markets being
thin — and so nobody is tempted to widen the series restriction after seeing
block counts.

### Integration gap found and closed

The coverage layer selected a date *and* a preferred series, but the anchor
layer took the earliest feasible occurrence **across the whole pool** regardless
of series. Diversity would never have materialised — coverage would keep asking
for baseball and the anchor would keep returning whatever started soonest. The
anchor scheduler now accepts a coverage-supplied series restriction. Series
membership is a design quantity like the target bin and the calendar, not an
activity signal, and the module still cannot see price, volume or wire activity
for the series it is handed.

### Session 03 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 — PASS.** `capped_time`. `events_received == events_archived == 34,972`.
Malformed, rejected, rotation failures, sequence faults, reconnects: **0**.
6 segments. `peak_1s_sliding` **1,042** vs 3,500.

**L2 — PASS.** First tick at open+300 s, gaps exactly `[300.0]`, reason
vocabulary closed, no ticker/volume in eligibility. **Max panel size 6**, not
12 — bounded by *availability*, exactly as the 8-market diversity restriction
predicted.

**L3 — PASS**, with a caveat that matters more than the pass. Schema
`row-v2`/`label-v2`, 13/17 columns, **no always-missing columns**,
`dataset_role=CONFIRMATION`, 9,331 rows, 0 dispatch errors — but **minimum M0
completeness 0.2509** and label coverage **0.251 / 0.249 / 0.241 / 0.170**
against ~0.99 in S02.

**L4 — earns the bin.**

| | S01 | S02 | **S03** |
|---|---:|---:|---:|
| covering intervals | 377 | 24 | **12** |
| counts toward bin | yes | yes | **yes** |
| market-blocks | 377 | 396 | **31** |
| clusters | 21 | 21 | **6** |
| markets clearing the floor | 24/24 | 23/24 | **6/8** |

Two markets (`KXMLBGAME-26AUG241840TBDET-DET`/`-TB`) were **naturally closed or
resolved** mid-session. Zero stayed open and failed activity eligibility.

### The finding: post-event baseball is not post-event tennis

The books went quiet as the games ended. Segment write times run
`01:51 → 01:59 → 02:15 → 02:30 → 02:45 → 02:51` with sizes collapsing
`2.2M → 1.3M → 1.8M → 396K → 216K → 108K`, and **the last frame arrived at
~02:51Z — 1 h 54 m before the session's scheduled end**. The capture stayed
alive and correctly recorded a quiet venue rather than truncating: a quiet
venue is a measurement.

That is a real venue-structure fact, and it **contradicts the S01 result within
the same stratum**. S01's post-event *tennis* books were dense — 24 of 24
markets cleared the floor, 377 blocks. S03's post-event *baseball* books
decayed to nothing. `late_resolution` is not one regime; it depends on the
sport's settlement behaviour. Recorded now because it will otherwise look, at
analysis time, like a property of the bin rather than of the series.

**The low completeness has a mechanical cause worth stating.** Rows are emitted
only for a publishable book, but a publishable book can be **one-sided** — and
a quiet post-event book frequently is. With no bid or no ask there is no mid,
no spread and no microprice, so those columns are absent and `design()` will
drop the row. **S03's usable sample is ~25% of its 9,331 rows, roughly 2,300**,
not the row count.

### Tranche ledger after S03

| bin | target | complete | remaining |
|---|---:|---:|---:|
| `late_resolution` | 4 | **2** | 2 |
| `live_event` | 4 | 1 | 3 |
| `near_event` | 4 | 0 | 4 |
| `approaching` | 4 | 0 | 4 |
| `far` | 4 | 0 | 4 |

| quantity | accumulated | floor | note |
|---|---:|---:|---|
| market-blocks | **804** (377+396+31) | 4,000 | S03 added 31, not ~280 as projected |
| clusters | **48** (21+21+6) | 150 | |
| series represented | **3 of 8** (`KXWTAMATCH`, `KXATPMATCH`, `KXMLBGAME`) | ≥6 | diversity advanced |
| weekend sessions | **1** | ≥4 | S03 was Sunday 21:45 ET → **weekday** |

**The 280-block projection for S03 was wrong by ~9×.** It assumed 8 markets ×
35 intervals. What actually bound the session was neither K nor the market
count but **the venue going quiet**, which no projection anticipated because
S01 had shown the opposite in the same bin.

---

## How to read S05 — written before the session closes

**Lifecycle compatibility and activity density are different claims, and S05
can only speak to one of them.**

The lifecycle rule claims exactly this: `KXATPMATCH × late_resolution` is
*structurally capable* of remaining open after `occurrence_datetime`, because
ATP contracts settle a median **+3.82 h** past that anchor. It claims **nothing**
about how much those markets will trade once open. A compatible stratum can be
quiet.

### The interpretation ladder

| S05 outcome | reading |
|---|---|
| candidates already **closed at launch** | a **lifecycle/preflight** problem — the rule or the guard is wrong |
| candidates **open** but fail the 30-events/300 s gate | **structurally compatible, operationally quiet** — the lifecycle rule worked |
| **some** markets qualify | a valid **sparse** late-resolution session |
| **dense/full** panels | a valid **dense** late-resolution session |

Only the first row is evidence against the lifecycle rule. The second is
consistent with it: the markets were reachable and simply had little trade.

### What must not happen

**No comparison with S01 may alter any rule.** I had framed S05 as "should look
like S01 rather than S03/S04", which quietly promotes a density comparison into
a test of a compatibility claim. It is not one. S05 may earn its bin weakly, or
fail the activity gate entirely, without invalidating lifecycle compatibility —
and it may look dense without confirming anything beyond that one slate.

If S05 fails on activity, the frozen contingency rule applies exactly as it did
for S04: the session stays in the corpus, is never relabelled, and its
obligation goes to S21+. **The lifecycle table is not re-derived from it.** That
table was measured from settlement metadata over 200 markets per series and is
not a hypothesis this session tests.

### Session 05 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 — PASS.** `capped_time`. `events_received == events_archived == 367,003`.
Malformed, rejected, rotation failures, sequence faults, reconnects: **0**.
33 segments. `peak_1s_sliding` **901** vs 3,500. Capture commit
`c7a7c965…` — exactly the pinned commit, verified at launch.

**L2 — PASS**, not vacuous. **35 ticks**, gaps exactly `[300.0]`, K respected,
closed reason vocabulary. The final two ticks were empty under the
session-remaining gate, as designed.

**L3 — PASS**, not vacuous. `row-v2`/`label-v2`, 13/17 columns, no
always-missing columns, `dataset_role=CONFIRMATION`, **29,700 rows**, 0 dispatch
errors, minimum M0 completeness **0.8262**. Label coverage
**0.826 / 0.825 / 0.819 / 0.783**.

**L4 — earns the bin.** **99 covering intervals** wholly inside
`late_resolution`; 99 market-blocks; 8 clusters; only `late_resolution` touched.

### Reading it against the pre-registered ladder

The ladder written before the session closes has four rungs. S05 lands on the
third: **a valid sparse late-resolution session.**

* candidates were **not** closed at launch — preflight recorded **24/24 live**,
  so this is not a lifecycle or preflight failure;
* **8 of 24** markets cleared the 30-events/300 s floor;
* **16** were *naturally closed or resolved during the session*;
* **0** stayed open and failed the activity gate.

**No comparison with S01 is drawn, and no rule is re-derived from this
session**, per the ladder.

One observation worth recording without acting on it: ATP's measured settlement
lag is a **median** of +3.82 h, so a 3 h session anchored at occurrence+5 min
sits inside a *distribution* of settlement times, and markets drop out
progressively as they settle. Sixteen did. That is consistent with the lifecycle
table rather than evidence against it — the table says the stratum is reachable,
not that every market survives the whole window. **The table is not re-measured
from this session.**

### Tranche ledger after S05

| bin | target | counted | remaining |
|---|---:|---:|---:|
| `late_resolution` | 4 | **3** | 1 |
| `live_event` | 4 | 1 | 3 |
| `near_event` | 4 | 0 | 4 |
| `approaching` | 4 | 0 | 4 |
| `far` | 4 | 0 | 4 |

| quantity | value | floor |
|---|---:|---:|
| sessions in corpus | **5** | — |
| sessions counted | **4** | — |
| market-blocks | **903** (377+396+31+99) | 4,000 |
| clusters | **56** (21+21+6+8) | 150 |
| series represented | **3 of 8** | ≥6 |
| weekend sessions | **1** | ≥4 |
| replacement obligations | **1** (S04 → S21+) | — |

### Session 06 — verdict: **OPERATIONALLY CLEAN**, counts toward `late_resolution`

**L1 — PASS.** `capped_time`. `events_received == events_archived ==
1,604,010`. Malformed, rejected, rotation failures, sequence faults,
reconnects: **0**. 125 segments. `peak_1s_sliding` **1,676** vs 3,500. Capture
commit `d7cfede…`, and the post-sleep drift guard logged
`re-verified at launch: d7cfede1ac6b` — its first production firing, after a
15-hour wait.

**L2 — PASS**, not vacuous. **35 ticks**, gaps exactly `[300.0]`, K respected.

**L3 — PASS**, not vacuous. 118,800 rows, minimum M0 completeness **0.966**, no
always-missing columns. Label coverage **0.975 / 0.974 / 0.973 / 0.963**.

**L4 — earns the bin.** **396 covering intervals**, 396 market-blocks, 22
clusters. **24 of 24 markets cleared the activity floor**; none closed
mid-session; none were too quiet. The only two markets without rows lost a
top-12 competition.

Rung four of the ladder: a **dense** late-resolution session. Per the rule
recorded before it closed, that validates nothing the table did not already
claim — the table says the stratum is *reachable*, and S05 and S06 are both
consistent with it at different densities.

Observed and **not acted on**: S05 anchored at 21:00Z and lost 16 of 24 markets
to settlement mid-session; S06 anchored at 18:00Z and lost none. Same series,
same rule, different points in the settlement distribution. **The lifecycle
table is not re-measured from this.**

---

## Tranche state after S06 — two separate facts

### 1. Bin quota

| bin | target | counted | remaining |
|---|---:|---:|---:|
| `late_resolution` | 4 | **4** | **0 — quota met** |
| `live_event` | 4 | 1 | 3 |
| `near_event` | 4 | 0 | 4 |
| `approaching` | 4 | 0 | 4 |
| `far` | 4 | 0 | 4 |

### 2. Replacement debt — **NOT cancelled by the quota being met**

| session | bin | discharged by |
|---|---|---|
| `MMEDGE-S04-late_resolution-20260826` | `late_resolution` | **S21** |

**`late_resolution` reads 4/4 AND S04 still owes a replacement.** These are
different facts. S05 and S06 discharged their **own** scheduled obligations,
not S04's, so the corpus legitimately ends with **five counted
`late_resolution` sessions** once S21 runs.

This distinction is now encoded, not merely written down. `coverage_deficit()`
previously inferred the requirement from slot arithmetic
(`obligations_total − planned_sessions_remaining`), which returned the right
number while S04 was the only miss but **named no session** and derived a
per-session debt from a bin-level quantity. Any later shift in the counts could
have cancelled a real debt silently, and a scheduler reading it would have
concluded S21 was unnecessary. `replacement_debt` is now a list of
`(session, bin)` attached to the missed sessions themselves.

### 3. Power and coverage

| quantity | value | floor |
|---|---:|---:|
| sessions in corpus / counted | 6 / **5** | — |
| market-blocks | **1,299** (377+396+31+99+396) | 4,000 |
| clusters | **78** (21+21+6+8+22) | 150 |
| series represented | **3 of 8** (`KXWTAMATCH` 2, `KXATPMATCH` 2, `KXMLBGAME` 1) | ≥6 |
| weekend sessions | **1** | ≥4 |

**Next obligation: `live_event`** — which carries no lifecycle restriction, so
all eight series are available and the coverage layer can finally advance
diversity mechanically.
